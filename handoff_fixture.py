"""DETERMINISTIC DATA-ONLY HANDOFF FIXTURE. No listener, Codex, or native inference."""
import argparse,json,os,time,uuid
from pathlib import Path
from portable import Deployment,QueueError,FileQueue,protocol,read,encode,init
from file_queue import request_for_text
import probe

def prepare(root,owner):
 init(root,owner,seconds=300,max_requests=1);d=Deployment(root)
 probe.observe(d,'desktop');probe.offer(d);probe.observe(d,'broker');probe.answer(d);probe.verify(d)
 q=FileQueue(d.queue_root,create=True,owner=owner,session='session_'+d.m['run_id'],max_jobs=1)
 try:
  nonce=uuid.uuid4().hex;req=request_for_text('SYNTHETIC_NONCE:'+nonce);j=q.enqueue(req,owner=owner,session=q.meta['session'],timeout=240)
  protocol.atomic_json(d.queue_root/'ready.json',{'version':1,'instance':uuid.uuid4().hex,'started':time.time(),'deadline':d.m['expires'],'session_mapping':'codex-0.159.2-hyphen-pair-v1','synthetic_fixture_no_listener':True})
  fixture={'contract':d.m['contract'],'deployment_id':d.m['deployment_id'],'job_id':j['id'],'nonce':nonce,'expected':'SYNTHETIC_REPLY:'+nonce.upper(),'no_native_inference':True,'no_http_listener':True}
  protocol.atomic_json(d.root/'evidence/synthetic-fixture.json',fixture,exclusive=True);return {'ready':True,'deployment_id':d.m['deployment_id'],'scope':'synthetic data fixture only','root':str(d.root)}
 finally:q.close()

def reply(d,input_path,result_path):
 f=read(d.root/'evidence/synthetic-fixture.json',8192);request=read(input_path,1500000)
 if f['deployment_id']!=d.m['deployment_id'] or request['deployment_id']!=d.m['deployment_id'] or request['job_id']!=f['job_id'] or request['request']!=request_for_text('SYNTHETIC_NONCE:'+f['nonce']):raise QueueError('not_this_synthetic_fixture')
 result={'kind':'message','text':f['expected']};protocol.atomic_json(result_path,result,exclusive=True);return {'saved_synthetic_result':True,'actual_native_inference':False}

def verify(d):
 f=read(d.root/'evidence/synthetic-fixture.json',8192);q=d.queue()
 try:r=q.get(f['job_id'],owner=d.m['owner_id'],session=q.meta['session'])
 finally:q.close()
 if r['state']!='completed' or r['result']!={'kind':'message','text':f['expected']} or r['delivery']!='waiting':raise QueueError('fixture_not_completed')
 out={'passed':True,'deployment_id':d.m['deployment_id'],'job_id':f['job_id'],'state':r['state'],'delivery':r['delivery'],'meaning':'queue acceptance only; this fixture has no HTTP consumer or native inference','actual_native_inference':False}
 protocol.atomic_json(d.root/'evidence/handoff-verified.json',out);return out

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('operation',choices=['prepare','reply','verify']);p.add_argument('--owner');p.add_argument('--input');p.add_argument('--result');a=p.parse_args();os.umask(0o077)
 if a.operation=='prepare':out=prepare(a.root,a.owner)
 elif a.operation=='reply':out=reply(Deployment(a.root),Path(a.input),Path(a.result))
 else:out=verify(Deployment(a.root))
 print(encode(out).decode())
if __name__=='__main__':
 try:main()
 except (QueueError,OSError,ValueError,KeyError,TypeError) as e:print(json.dumps({'error':getattr(e,'code',type(e).__name__)}));raise SystemExit(1)
