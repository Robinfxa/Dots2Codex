"""Opt-in Docs-CAS wrappers for the independent immutable-message facade.

Direct control references replace folder discovery. No native invocation is made
here; a fresh successful begin is still a one-use permit for the admitted caller.
"""
import copy
from .model import Object, canonical, hash_bytes, require, ProtocolError
from .session import Controller, Worker
from .control import binding_for, _ref


class ControlReferenceView:
    """Snapshot-pinned reachable graph; never scans a Drive folder."""
    def __init__(self,messages,control,pin):self.messages,self.control,self.pin=messages,control,pin;self.cache={}
    def reserve(self):return self.messages.reserve()
    def publish(self,obj,reservation):return self.messages.publish(obj,reservation)
    def reference(self,obj,publication):return self.messages.reference(obj,publication)
    def fetch(self,reference):return self.messages.fetch(reference)

    def scan(self,deployment_id):
        require(deployment_id==self.pin.body['identity']['deployment_id'],'control_view_deployment_mismatch')
        s=self.control.read().state
        require(s['binding']==binding_for(self.pin),'control_stale_worker_binding')
        refs={}
        for record in [s]+s['history']:
            if record['binding']['deployment_hash']!=self.pin.oid:continue
            if record['request'] is not None:
                ref=_ref(record['request']);refs[ref['object_id']]=ref
            if record['result'] is not None:
                result=record['result'];ref=result['reference'];refs[ref['object_id']]=ref
                for ref in result['dependencies'].values():refs[ref['object_id']]=ref
            if record['receipt'] is not None:
                ref=record['receipt'];refs[ref['object_id']]=ref
        out=[]
        for ref in refs.values():
            key=canonical(ref)
            if key not in self.cache:self.cache[key]=self.messages.fetch(ref)
            out.append(self.cache[key])
        return out


class _Controlled:
    def _setup(self,journal,messages,coordinator):
        self.coordinator=coordinator;self.messages=messages
        require(coordinator.messages is messages,'control_message_store_mismatch')
        self._binding=binding_for(journal.pin)
        snapshot=coordinator.store.read()
        require(snapshot.state['binding']==self._binding,'control_stale_worker_binding')
        self.control_id=snapshot.state['control_id']
        self._control_mode={'document_id':snapshot.document_id,'tab_id':snapshot.tab_id,
                            'control_id':self.control_id,'deployment_hash':journal.pin.oid}
        with journal.locked() as state:
            if state['control_mode'] is None:
                require(not state['requests'] and not state['executions'],'cas_enrollment_requires_fresh_journal')
                state['control_mode']=self._control_mode
                journal.save(state)
            else:
                require(state['control_mode']==self._control_mode,'control_mode_pin_mismatch')
        return ControlReferenceView(messages,coordinator.store,journal.pin)

    def _operation(self,kind,request_id):
        return hash_bytes(canonical({'control_id':self.control_id,'kind':kind,'request':request_id}))

    def _published_reference(self,object_id):
        with self.journal.locked() as s:
            require(object_id in s['objects'] and object_id in s['published'],'message_not_published')
            obj=Object.parse(canonical(s['objects'][object_id]))
            return self.messages.reference(obj,s['published'][object_id])


class CASController(_Controlled,Controller):
    def __init__(self,journal,messages,coordinator):
        view=self._setup(journal,messages,coordinator)
        super().__init__(journal,view)

    def _submit_payload(self,payload,idempotency_key):
        current=self.coordinator.store.read().state
        require(current['binding']==self._binding,'control_stale_worker_binding')
        require(not current.get('closed',False),'control_session_closed')
        oid=super()._submit_payload(payload,idempotency_key)
        reference=self._published_reference(oid)
        self.coordinator.transition('admit',{'request':reference},self._operation('admit',oid))
        return oid

    def close_session(self):
        return self.coordinator.transition('close',{'binding':self._binding},
            self._operation('close',self.pin.oid))

    def result(self,request_id):
        s=self.coordinator.store.read().state
        require(s['binding']==self._binding,'control_stale_worker_binding')
        expected=None
        for record in [s]+s['history']:
            if record['request'] and record['request']['object_id']==request_id and record['result']:
                expected=record['result']['reference']['object_id']
        if expected is None:return None
        obj=super().result(request_id)
        require(obj is not None and obj.oid==expected,'control_result_mismatch')
        return obj

    def record_delivery(self,request_id,result_id,evidence):
        oid=super().record_delivery(request_id,result_id,evidence)
        reference=self._published_reference(oid)
        self.coordinator.transition('receipt',{'binding':self._binding,'receipt':reference},
                                    self._operation('receipt',request_id))
        return oid


class CASWorker(_Controlled,Worker):
    def __init__(self,journal,messages,coordinator):
        view=self._setup(journal,messages,coordinator)
        super().__init__(journal,view)

    def start_next(self):
        snapshot=self.coordinator.store.read();s=snapshot.state
        require(s['binding']==self._binding,'control_stale_worker_binding')
        if s.get('closed',False) or s['phase'] in {'IDLE','RESULT_COMMITTED','DELIVERED'}:return None
        require(s['phase']=='REQUESTED','control_execution_or_claim_requires_reconciliation')
        # Parent implementation durably burns the local marker before exposing input.
        permit=super().start_next()
        if permit is None:return None
        require(permit['request_id']==s['request']['object_id'],'control_request_mismatch')
        claim_id=self._operation('claim',permit['request_id'])
        self.coordinator.transition('claim',{'binding':self._binding,'claim_id':claim_id},claim_id,snapshot=snapshot)
        begun=self.coordinator.transition('begin',{'binding':self._binding,'claim_id':claim_id,
            'dispatch_id':permit['dispatch_id']},self._operation('begin',permit['request_id']))
        require(begun['status']=='applied' and begun['permit'] is not None,'control_begin_permit_unavailable')
        self._live()
        permit['control']=begun['permit']
        return permit

    def complete(self,permit,text):
        require(isinstance(permit,dict) and isinstance(permit.get('control'),dict),'control_permit_required')
        grant=permit['control']
        require(grant=={'control_id':self.control_id,'request_id':permit.get('request_id'),
                'generation':self._binding['generation'],'native_task_id':self._binding['native_task_id'],
                'dispatch_id':permit.get('dispatch_id'),'operation_id':self._operation('begin',permit.get('request_id'))},
                'control_permit_mismatch')
        s=self.coordinator.store.read().state
        require(s['binding']==self._binding and s['request'] is not None and
                s['request']['object_id']==permit['request_id'] and s['dispatch'] is not None and
                s['dispatch']['id']==permit['dispatch_id'] and
                s['phase'] in {'DISPATCH_INTENT','AMBIGUOUS','RESULT_COMMITTED','DELIVERED'},'control_unknown_dispatch')
        oid=super().complete(permit,text)
        with self.journal.locked() as state:
            result=Object.parse(canonical(state['objects'][oid]))
            started=Object.parse(canonical(state['objects'][result.body['links']['started']]))
        refs={'request':_ref(s['request']),'claim':self._published_reference(started.body['links']['claim']),
              'started':self._published_reference(started.oid),'result':self._published_reference(oid)}
        self.coordinator.transition('result',{'binding':self._binding,'dispatch_id':permit['dispatch_id'],**refs},
                                    self._operation('result',permit['request_id']))
        return oid
