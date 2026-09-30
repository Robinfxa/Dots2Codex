"""Local Mac operator commands for an already-running long-session facade.

Reads this endpoint's own private ready/journal files, never credentials. Result
retrieval is read-only; only explicit ack creates a delivery receipt. This helper
prints a Codex command but never starts a model or a native worker.
"""
import argparse
import http.client
import json
import shlex
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from .backend import read_private_file
from .model import require


def codex_command(base_url,workdir):
    url=urlsplit(base_url)
    require(url.scheme=='http' and url.hostname=='127.0.0.1' and url.port and url.path=='/v1' and
            not url.username and not url.query and not url.fragment,'loopback_ready_url_required')
    provider='{name="Dots remote",base_url='+json.dumps(base_url)+',wire_api="responses",requires_openai_auth=false,supports_websockets=false,request_max_retries=0,stream_max_retries=0,stream_idle_timeout_ms=120000}'
    return ['codex','--cd',str(Path(workdir).expanduser()),'--model','native-subagent-bridge',
        '--ask-for-approval','on-request','--sandbox','workspace-write',
        '-c','model_provider="dots_remote"','-c','model_providers.dots_remote='+provider,
        '-c','features.enable_request_compression=false',
        '-c','features.multi_agent=false','-c','features.multi_agent_v2=false',
        '-c','features.unbounded_connection_retries=false','-c','web_search="disabled"',
        '-c','features.image_generation=false','-c','features.computer_use=false']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation',choices=['codex-command','check-codex','status','result','ack','close'])
    p.add_argument('--codex',default='codex');p.add_argument('--ready',required=True);p.add_argument('--journal');p.add_argument('--workdir',default='.')
    p.add_argument('--request-id');p.add_argument('--result-id');p.add_argument('--evidence');p.add_argument('--confirm',action='store_true')
    a=p.parse_args();ready=json.loads(read_private_file(a.ready,131072));base=ready['base_url']
    command=codex_command(base,a.workdir)
    command[0]=a.codex
    if a.operation=='check-codex':
        version=subprocess.run([a.codex,'--version'],capture_output=True,text=True,timeout=10,check=True)
        help_result=subprocess.run([a.codex,'--help'],capture_output=True,text=True,timeout=10,check=True)
        required=['--config','--model','--cd','--sandbox','--ask-for-approval']
        missing=[flag for flag in required if flag not in help_result.stdout]
        require(not missing,'codex_required_flags_missing:'+','.join(missing))
        return {'version':version.stdout.strip(),'required_flags_present':True,'inference_calls':0,
                'provider_transport_tested':False,'mac_tool_loop_tested':False,
                'source_reference':'rust-v0.159.2; different versions require live acceptance'}
    if a.operation=='codex-command':return {'command':shlex.join(command),'automatic_wake':False,
        'scope':ready.get('scope'),'expires':ready.get('expires'),'max_requests':ready.get('max_requests'),
        'tested_protocol_source':'Codex rust-v0.159.2; verify your installed codex --version before live acceptance',
        'note':'Run this in the authorized Mac workspace after the matching native worker is actively polling'}
    require(a.journal is not None,'controller_journal_required')
    state=json.loads(read_private_file(Path(a.journal)/'remote-facade-state.json',131072))
    identity=state['binding'] or {'session-id':'operator-before-first-turn','thread-id':str(uuid.uuid4())}
    headers={**identity,'Content-Type':'application/json'}
    method='GET';body=None
    if a.operation=='status':path='/v1/bridge/status'
    elif a.operation=='close':
        require(a.confirm,'explicit_close_required');path='/v1/bridge/close';method='POST';body={'confirm':True}
    else:
        require(a.request_id and len(a.request_id)==64 and all(x in '0123456789abcdef' for x in a.request_id),'request_id_required')
        path='/v1/bridge/requests/'+a.request_id
        if a.operation=='result':path+='/result'
        else:
            require(a.result_id and a.evidence,'exact_result_and_observation_evidence_required')
            path+='/ack';method='POST';body={'result_id':a.result_id,'evidence':a.evidence}
    url=urlsplit(base);connection=http.client.HTTPConnection(url.hostname,url.port,timeout=60)
    try:
        connection.request(method,path,body=None if body is None else json.dumps(body),headers=headers)
        response=connection.getresponse();raw=response.read(2*1024*1024+1)
        require(len(raw)<=2*1024*1024,'operator_response_too_large')
        return {'http_status':response.status,'response':json.loads(raw)}
    finally:connection.close()


if __name__=='__main__':print(json.dumps(main(),ensure_ascii=False,indent=2))
