"""Private facade launch gate. No facade/Google import until durable PID grant.

A supervisor crash between Popen and PID persistence leaves only this bounded
waiting child, which exits without opening the facade. The wrapper keeps its OS
command identity when loading the reviewed CLI in-process.
"""
import argparse
import json
import os
import runpy
import stat
import sys
import time
from pathlib import Path
from .backend import read_private_file
from .model import require


def await_grant(runtime, timeout=10):
    runtime = Path(runtime)
    require(not runtime.is_symlink() and runtime.is_dir(), 'unsafe_router_runtime')
    info = runtime.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077 and stat.S_ISDIR(info.st_mode),
            'unsafe_router_runtime')
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        require(not (runtime / 'stop-requested.json').exists(), 'router_start_cancelled')
        grant = runtime / 'facade-launch.json'
        if grant.exists():
            value = json.loads(read_private_file(grant, 131072))
            require(value['pid'] == os.getpid() and value['session_id'] == runtime.name,
                    'router_launch_grant_mismatch')
            return
        time.sleep(.05)
    raise RuntimeError('router_launch_grant_timeout')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime', required=True)
    p.add_argument('arguments', nargs=argparse.REMAINDER)
    a = p.parse_args(); os.umask(0o077)
    args = a.arguments[1:] if a.arguments[:1] == ['--'] else a.arguments
    require(args[:1] == ['serve'], 'router_child_only_serves')
    await_grant(a.runtime)
    sys.argv = ['remote_transport.cli', *args]
    runpy.run_module('remote_transport.cli', run_name='__main__')


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'error': type(exc).__name__}), flush=True)
        raise SystemExit(1)
