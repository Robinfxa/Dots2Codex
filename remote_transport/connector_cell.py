"""Generate trusted active-native JS cells, offline. Never executes connector tools.

Read only this release's static JS. Config/paths are JSON data, never executable
request text. The actual admitted native worker executes the generated cell.
"""
import argparse
import json
import os
from pathlib import Path
from .backend import read_private_file
from .cli import write_new
from .model import require


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('phase',choices=['claim-begin','upload-commit'])
    p.add_argument('--root',required=True);p.add_argument('--native-task-id',required=True)
    p.add_argument('--manifest',required=True);p.add_argument('--seq',type=int)
    p.add_argument('--save',required=True)
    a=p.parse_args();os.umask(0o077)
    from .connector_worker import ConnectorWorker
    w=ConnectorWorker(a.root,a.native_task_id)
    release=Path(__file__).resolve().parents[1]
    config={'cwd':str(release),'root':str(w.root.resolve()),'nativeTaskId':a.native_task_id,
            'manifest':str(Path(a.manifest).resolve()),'documentId':w.config['document_id']}
    require(a.phase!='upload-commit' or type(a.seq) is int and a.seq>0,'sequence_required')
    prefix='// @exec: {"yield_time_ms": 1000, "max_output_tokens": 2000}\n'
    js='\n'.join((release/'native_connector'/name).read_text() for name in ('runner.js','tool_adapter.js'))
    invocation=("await runner.claimAndBegin({evidence: config.manifest})" if a.phase=='claim-begin'
                else 'await runner.uploadAndCommit({seq: '+str(a.seq)+'})')
    suffix='\nconst config='+json.dumps(config,ensure_ascii=False)+';\n'
    suffix+='const adapter=createNativeToolAdapter(tools,config);\n'
    suffix+='const runner=createNativeConnectorRunner(adapter.io,{now:adapter.now});\n'
    # Output summary only. Complete responses/evidence remain private on disk.
    suffix+='const outcome='+invocation+';\n'
    suffix+='text(outcome);\n'
    write_new(Path(a.save),(prefix+js+suffix).encode())
    return {'cell_saved':a.save,'phase':a.phase,'native_execution_required':True,'connector_calls':0}


if __name__=='__main__':print(json.dumps(main()))
