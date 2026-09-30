#!/usr/bin/env python3
"""Bounded active-broker wait: heartbeat/challenge acknowledgment plus one claim.
The caller must be an available, authorized inference broker. No model or tools are run here.
"""
import argparse,math,time
from pathlib import Path
from core import FileQueue,QueueError,protocol,read
from readiness import broker_tick,valid_id

def wait_for_job(root,wait=20,lease=180):
 if not 0<=wait<=20 or not .1<=lease<=180:raise QueueError('invalid_limit')
 root=Path(root);ready=read(root/'ready.json',8192)
 if not isinstance(ready,dict) or ready.get('version')!=1 or ready.get('session_mapping')!='codex-0.159.2-hyphen-pair-v1' or type(ready.get('deadline')) not in (int,float) or not math.isfinite(ready['deadline']):raise QueueError('invalid_service_metadata')
 instance=valid_id(ready.get('instance'));q=FileQueue(root);scope={'owner':q.meta['owner'],'session':q.meta['session']};end=time.monotonic()+wait;claimed=False
 try:
  while True:
   remaining=ready['deadline']-time.time()
   if remaining<=0 or (root/'closed.json').exists() or (root/'stop.requested').exists():return None
   broker_tick(root,instance,'ready')
   job=q.claim(**scope,wait=0,lease_seconds=min(lease,max(.05,remaining)))
   if job is not None:claimed=True;broker_tick(root,instance,'busy');return job
   if time.monotonic()>=end:return None
   time.sleep(min(.05,max(0,end-time.monotonic())))
 finally:
  try:
   if not claimed:broker_tick(root,instance,'closed')
  finally:q.close()
if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',required=True);p.add_argument('--wait',type=float,default=20);p.add_argument('--lease',type=float,default=180);p.add_argument('--save');a=p.parse_args();job=wait_for_job(a.root,a.wait,a.lease)
 if a.save:protocol.atomic_json(Path(a.save),job)
 print(protocol.encode(job).decode());raise SystemExit(0 if job is not None else 2)
