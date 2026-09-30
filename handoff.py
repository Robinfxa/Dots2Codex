"""Print a versioned data-only role assignment packet for a supported driver."""
import argparse,json,re,sys
from pathlib import Path
from portable import Deployment,QueueError,read,encode

def packet(d,owner,assignment,package_path):
 d.scope(owner);path=Path(assignment).absolute()
 try:relative=path.relative_to(d.root)
 except ValueError:raise QueueError('assignment_outside_deployment') from None
 if not re.fullmatch(r'evidence/[A-Za-z0-9_.-]+\.json',str(relative)):raise QueueError('assignment_path_not_evidence_relative')
 a=read(path,8192)
 with d.locked() as s:d.current(s,a)
 return {'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'run_id':d.m['run_id'],'owner_id':owner,'role':a['role'],'local_package_path':str(Path(package_path).absolute()),'local_deployment_root':str(d.root),'assignment_relative_path':str(relative),'instructions':'BROKER_HANDOFF.zh-CN.md' if a['role']=='broker' else 'START_HERE.zh-CN.md','expires':d.m['expires'],'max_requests':d.m['policy']['max_requests'],'scope':'text_only','native_inference':a['native_capability'],'parent_notification':d.m['notification'],'automatic_task_spawn':False}
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--owner',required=True);p.add_argument('--assignment',required=True);p.add_argument('--package-path',default=str(Path(__file__).resolve().parent));a=p.parse_args()
 try:print(encode(packet(Deployment(a.root),a.owner,a.assignment,a.package_path)).decode())
 except (QueueError,OSError,ValueError,TypeError) as e:print(json.dumps({'error':getattr(e,'code',type(e).__name__)}),file=sys.stderr);raise SystemExit(1)
