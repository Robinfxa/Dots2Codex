"""OFFLINE fixture only. No native task, Google account, client tool, or inference.

Exercises the real isolated CASWorker/RemoteResponsesFacade beneath the gateway.
The in-memory control store and synthetic admission receipts are intentionally
not production integrations and can never make ready_for_config true.
"""
import concurrent.futures
import copy
import http.client
import json
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import uuid

from .backend import LocalFSBackend
from .control import ControlSnapshot, CASConflict, block_for, initial_state, SessionCoordinator
from .controlled import CASController, CASWorker
from .facade import RemoteResponsesFacade
from .global_gateway import Store, Gateway, control_call, private_dir, probe
from .model import canonical, deployment, require
from .selection import admission_receipt, load_catalog, select
from .session import Journal


class FixtureControl:
    def __init__(self,pin):
        self.block=block_for(initial_state(pin,'offline-'+pin.body['identity']['session_id']))
        self.revision=1;self.lock=threading.Lock()
    def read(self):
        with self.lock:return ControlSnapshot('offline-doc','offline-tab','offline-writer',str(self.revision),self.block,time.monotonic())
    def compare_and_swap(self,snapshot,new_state):
        with self.lock:
            if snapshot.revision_id!=str(self.revision) or snapshot.block!=self.block:raise CASConflict('fixture_cas_conflict')
            self.block=block_for(new_state);self.revision+=1
            return copy.deepcopy(new_state)


def unused_fixture_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def request(text,model='gpt-6.1-sol',effort='high',history=(),tools=()):
    return {'model':model,'reasoning':{'effort':effort},'stream':True,'tools':list(tools),
            'input':list(history)+[{'type':'message','role':'user','content':[{'type':'input_text','text':text}]}]}


def identity():return {'session-id':'offline-fixture-'+secrets.token_hex(4),'thread-id':str(uuid.uuid4())}


def events(raw):return [json.loads(line[6:]) for line in raw.splitlines() if line.startswith(b'data: ')]


def post(store,generation,client,body,headers=None):
    conn=http.client.HTTPConnection('127.0.0.1',store.config()['port'],timeout=8)
    try:
        conn.request('POST',f'/activations/{generation}/v1/responses',body=canonical(body),
                     headers={**client,'Content-Type':'application/json',**(headers or {})})
        response=conn.getresponse();return response.status,response.read()
    finally:conn.close()


class OfflineController:
    def __init__(self,store,capacity=2):
        self.store=store;self.children={};self.spawn_count=0
        self.controller_id=secrets.token_hex(16)
        joined=control_call(store.root,'join',{'controller_id':self.controller_id,'mode':'offline_fixture','seconds':600,'capacity':capacity})
        self.credentials={'controller_id':self.controller_id,'epoch':joined['epoch']}
    def call(self,operation,extra=None):return control_call(self.store.root,operation,{**self.credentials,**(extra or {})})
    def claim(self):return self.call('claim')
    def admit_next(self):
        claimed=self.claim();require(claimed.get('pending'),'fixture_no_pending_route')
        rid=claimed['route_id'];owned={k:claimed[k] for k in ('route_id','claim','version')}
        started=self.call('begin',owned);owned['version']=started['version'];self.spawn_count+=1
        # A synthetic task ID. The fixture never calls collaboration.spawn_agent.
        task='/offline_fixture/'+started['spawn_arguments']['task_name']
        selected=claimed['selection'];receipt=admission_receipt(selected,started['spawn_arguments'],task)
        pin=deployment(rid,task,seconds=min(300,int(claimed['expires']-time.time())-1),max_requests=128,
                       scope='responses_tools',inference={'selection':selected,'admission':receipt})
        admitted=self.call('admit',{**owned,'pin':pin.value});owned['version']=admitted['version']
        directory=private_dir(self.store.root/'fixture-routes'/rid,create=True)
        backend=LocalFSBackend(directory/'messages',create=True)
        control=FixtureControl(pin);coordinator=SessionCoordinator(control,backend)
        controller=CASController(Journal.provision(directory/'controller',pin,'controller'),backend,coordinator)
        worker=CASWorker(Journal.provision(directory/'worker',pin,'worker'),backend,coordinator)
        facade=RemoteResponsesFacade(controller,long_session=True,request_deadline=4,poll_interval=.05,heartbeat_interval=.1).start()
        self.children[rid]={'pin':pin,'controller':controller,'worker':worker,'facade':facade,'owned':owned,
                            'identity':claimed['identity'],'generation':claimed['generation'],'selection':selected}
        self.call('attach',{**owned,'endpoint':facade.base_url,'worker_state':'WORKER_POLLING'})
        return rid
    def turn(self,rid,body,result):
        child=self.children[rid]
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(post,self.store,child['generation'],child['identity'],body)
            permit=child['worker'].poll(child['worker'].start_next,attempts=8,initial_delay=.02,max_delay=.1)
            require(permit is not None,'fixture_worker_not_dispatched')
            child['worker'].complete(permit,result);status,raw=future.result()
        require(status==200,'fixture_gateway_failed');return events(raw),permit
    def close(self):
        for child in self.children.values():child['facade'].close()


def main():
    with tempfile.TemporaryDirectory(prefix='dots-global-offline-') as directory:
        selected=select(load_catalog(),'gpt-6.1-sol','high')
        store,generation=Store.initialize(Path(directory)/'state',selected,port=unused_fixture_port())
        with Gateway(store):
            controller=OfflineController(store)
            try:
                clients=[identity(),identity()];bodies=[request('fixture A'),request('fixture B','gpt-6-astra','xhigh')]
                pending=[post(store,generation,c,b)[0] for c,b in zip(clients,bodies)]
                routes=[controller.admit_next(),controller.admit_next()]
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    outcomes=list(pool.map(lambda pair:controller.turn(pair[0],pair[1],'offline synthetic '+pair[0]),zip(routes,bodies)))
                require(all(ev[-1]['type']=='response.completed' for ev,_ in outcomes),'fixture_incomplete')
                print(json.dumps({'offline_only':True,'native_inference_calls':0,'google_writes':0,
                                  'pending_statuses':pending,'isolated_children':controller.spawn_count,
                                  'distinct_pins':len({c['pin'].oid for c in controller.children.values()}),
                                  'production_ready':probe(store.root)['production_ready']}))
            finally:controller.close()

if __name__=='__main__':main()
