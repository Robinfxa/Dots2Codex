"""Ephemeral service/broker readiness protocol. No credentials or inference requests."""
import hashlib,os,re,secrets,time
from pathlib import Path
from core import QueueError,protocol,read
HEX=re.compile(r'^[0-9a-f]{32}$')
FRESH_SECONDS=5

def valid_id(x):
 if not isinstance(x,str) or not HEX.fullmatch(x):raise QueueError('invalid_readiness_id')
 return x

def challenge(root,instance,wait=3):
 """Service asks its separately running broker for a fresh nonce acknowledgment."""
 valid_id(instance)
 if not .05<=wait<=5:raise QueueError('invalid_readiness_wait')
 root=Path(root);nonce=secrets.token_hex(16);created=time.time()
 protocol.atomic_json(root/'broker-challenge.json',{'version':1,'instance':instance,'nonce':nonce,'created':created,'deadline':created+wait})
 end=time.monotonic()+wait
 while time.monotonic()<end:
  try:answer=read(root/'broker-ack.json',4096)
  except FileNotFoundError:answer=None
  if isinstance(answer,dict) and answer.get('version')==1 and answer.get('instance')==instance and answer.get('nonce')==nonce:
   if answer.get('state')!='ready' or not isinstance(answer.get('at'),(float,int)) or not created<=answer['at']<=time.time()+1:raise QueueError('broker_ack_invalid')
   hb=read(root/'broker-heartbeat.json',4096)
   if hb.get('instance')!=instance or hb.get('state')!='ready' or not 0<=time.time()-hb.get('at',0)<=FRESH_SECONDS:raise QueueError('broker_heartbeat_stale')
   return {'challenge':nonce,'ack_at':answer['at'],'heartbeat_at':hb['at']}
  time.sleep(.01)
 raise QueueError('broker_challenge_timeout')

def broker_tick(root,instance,state='ready'):
 """Call from the active broker's bounded control loop; does not spawn a model."""
 valid_id(instance)
 if state not in ('ready','busy','closed'):raise QueueError('invalid_broker_state')
 root=Path(root);now=time.time();heartbeat={'version':1,'instance':instance,'state':state,'at':now,'pid':os.getpid()}
 protocol.atomic_json(root/'broker-heartbeat.json',heartbeat)
 if state!='ready':return heartbeat
 try:c=read(root/'broker-challenge.json',4096)
 except FileNotFoundError:return heartbeat
 if not isinstance(c,dict) or c.get('version')!=1 or c.get('instance')!=instance:return heartbeat
 valid_id(c.get('nonce'))
 if not isinstance(c.get('created'),(int,float)) or not isinstance(c.get('deadline'),(int,float)) or not c['created']<=now<=c['deadline'] or c['deadline']-c['created']>5:return heartbeat
 protocol.atomic_json(root/'broker-ack.json',{'version':1,'instance':instance,'nonce':c['nonce'],'state':'ready','at':now})
 return heartbeat
