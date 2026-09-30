#!/usr/bin/env python3
"""Bounded queue operations for an authorized inference-only broker. No payload execution."""
import argparse,os,sys
from pathlib import Path
from file_queue import FileQueue,QueueError,read,protocol

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=['claim','renew','complete','fail','status']);p.add_argument('--root',required=True);p.add_argument('--ticket',type=Path);p.add_argument('--result',type=Path);p.add_argument('--wait',type=float,default=0);p.add_argument('--lease',type=float,default=120);p.add_argument('--save',type=Path);a=p.parse_args();os.umask(0o077)
    q=FileQueue(a.root);scope={'owner':q.meta['owner'],'session':q.meta['session']}
    try:
        if a.operation=='claim':out=q.claim(**scope,wait=a.wait,lease_seconds=a.lease)
        else:
            if not a.ticket:raise QueueError('ticket_required')
            t=read(a.ticket);params=[t['id'],t['lease'],t['lease_epoch'],t['request_sha256']]
            if a.operation=='renew':out=q.renew(*params,**scope,seconds=a.lease)
            elif a.operation=='status':out=q.get(t['id'],**scope)
            else:
                if not a.result:raise QueueError('result_required')
                value=read(a.result,protocol.MAX_RESPONSE)
                out=q.complete(*params,**scope,**({'result':value} if a.operation=='complete' else {'error':value}))
        if a.save:protocol.atomic_json(a.save,out)
        print(protocol.encode(out).decode());return 0 if out is not None else 2
    finally:q.close()
if __name__=='__main__':
    try:raise SystemExit(main())
    except (QueueError,OSError,ValueError,KeyError) as e:print(protocol.encode({'error':getattr(e,'code',type(e).__name__)}).decode(),file=sys.stderr);raise SystemExit(1)
