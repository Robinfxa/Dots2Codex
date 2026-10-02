"""Synthetic offline tunnel+bridge used only by lifecycle tests. No MCP claims."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT.parent)]
from global_launcher import register_child_owner


def main():
    state = Path(os.environ['DOTS_DIRECT_OWNER_DIR']).parents[1]
    settings = json.loads((state / 'settings.json').read_text())
    bridge = '--fixture-bridge' in sys.argv
    process = None
    if bridge:
        os.environ.pop('CONTROL_PLANE_API_KEY', None)
        register_child_owner()
    else:
        # Exercise third-party stdout/stderr suppression with synthetic values.
        print('fixture output ' + os.environ['CONTROL_PLANE_API_KEY'], flush=True)
        print('fixture bearer ' + os.environ['DOTS_BRIDGE_HTTP_BEARER'], file=sys.stderr, flush=True)
        process = subprocess.Popen([sys.executable, __file__, '--fixture-bridge'], env=dict(os.environ))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if bridge:
                if self.headers.get('Authorization') != 'Bearer ' + os.environ['DOTS_BRIDGE_HTTP_BEARER']:
                    self.send_error(401)
                    return
                body = json.dumps({'mode': 'global', 'listener_ready': True,
                    'config_id': settings['config_id'], 'instance_id': os.environ['DOTS_DIRECT_RUN_ID']}).encode()
            else:
                body = b'ready'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    HTTPServer.allow_reuse_address = True
    server = HTTPServer(('127.0.0.1', settings['http_port' if bridge else 'admin_port']), Handler)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    while not stop.wait(.05):
        if process is not None and process.poll() is not None:
            break
    server.shutdown()
    server.server_close()
    if process is not None:
        process.terminate()
        process.wait(timeout=3)


if __name__ == '__main__':
    main()
