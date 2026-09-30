"""Data-only bounded shared-filesystem inference queue; never executes payloads."""
import contextlib, fcntl, hashlib, importlib.util, json, math, os, re, select, stat, time, uuid
from pathlib import Path
REF = Path(__file__).parent/'reference/bridge.py'
if hashlib.sha256(REF.read_bytes()).hexdigest() != '1e89ec2084ea2a8c81e1d1ca3f76e9b7262da0e98c2cdf0114fb52bd716911f9':
    raise RuntimeError('frozen_protocol_changed')
spec=importlib.util.spec_from_file_location('file_ipc_protocol', REF)
protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
TERMINAL={'completed','failed','expired','cancelled'}
ID=re.compile(r'^[0-9a-f]{32}$')
class QueueError(Exception):
    def __init__(self,code):super().__init__(code);self.code=code

def encode(v):
    try:return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    except (ValueError,TypeError,UnicodeError,RecursionError):raise QueueError('invalid_json') from None

def digest(v):return hashlib.sha256(encode(v)).hexdigest()
def checked(v):
    if not isinstance(v,str) or not ID.fullmatch(v):raise QueueError('invalid_id')
    return v

def number(v,lo,hi):
    if isinstance(v,bool) or not isinstance(v,(float,int)) or not lo<=v<=hi:raise QueueError('invalid_limit')
    return v

def read(path,limit=1100000):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise QueueError('not_regular_file')
            b=f.read(limit+1)
        if len(b)>limit:raise QueueError('payload_too_large')
        return protocol.decode(b)
    except (ValueError,UnicodeError,RecursionError):raise QueueError('invalid_json') from None

class FileQueue:
    """Shared POSIX local filesystem only. flock + rename + directory fsync required."""
    def __init__(self,root,*,create=False,owner='local-owner',session='local-session',max_jobs=32,poll_ms=10):
        supplied=Path(root).absolute()
        if supplied.is_symlink():raise QueueError('symlink_root')
        self.root=supplied.resolve();number(poll_ms,2,1000);self.poll=poll_ms/1000
        if create:
            number(max_jobs,1,512)
            if type(max_jobs) is not int:raise QueueError('invalid_limit')
            for v in (owner,session):
                if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v):raise QueueError('invalid_scope')
            self.root.mkdir(mode=0o700,parents=True,exist_ok=False)
            (self.root/'jobs').mkdir(mode=0o700)
            fd=os.open(self.root/'queue.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
            protocol.atomic_json(self.root/'meta.json',{'version':1,'owner':owner,'session':session,'max_jobs':max_jobs,'created':time.time()},exclusive=True)
        st=self.root.stat()
        if st.st_uid!=os.getuid() or st.st_mode&0o077:raise QueueError('runtime_not_private_owned')
        if (self.root/'jobs').is_symlink():raise QueueError('symlink_jobs')
        self.meta=read(self.root/'meta.json',4096)
        if not isinstance(self.meta,dict) or self.meta.get('version')!=1:raise QueueError('unsupported_version')
        if set(self.meta)!={'version','owner','session','max_jobs','created'} or type(self.meta['max_jobs']) is not int or not 1<=self.meta['max_jobs']<=512:raise QueueError('invalid_metadata')
        for v in (self.meta['owner'],self.meta['session']):
            if not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v):raise QueueError('invalid_metadata')
        self.lock_fd=os.open(self.root/'queue.lock',os.O_RDWR|os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(self.lock_fd).st_mode):os.close(self.lock_fd);raise QueueError('invalid_lock')
        self.closed=False
        self.wake=Wake(self.root/'jobs',self.poll)
    def close(self):
        if not self.closed:self.wake.close();os.close(self.lock_fd);self.closed=True
    @contextlib.contextmanager
    def locked(self):
        if self.closed:raise QueueError('queue_closed')
        # A separately opened lock FD per operation also serializes threads in this process.
        fd=os.open(self.root/'queue.lock',os.O_RDWR|os.O_NOFOLLOW)
        end=time.monotonic()+2
        try:
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=end:raise QueueError('lock_timeout')
                    time.sleep(.002)
            yield
        finally:os.close(fd)
    def path(self,jid):return self.root/'jobs'/(checked(jid)+'.json')
    def save(self,state):protocol.atomic_json(self.path(state['id']),state)
    def scope(self,owner,session):
        if owner!=self.meta['owner'] or session!=self.meta['session']:raise QueueError('scope_mismatch')
    def states(self):
        paths=sorted((self.root/'jobs').glob('*.json'))
        if len(paths)>self.meta['max_jobs']:raise QueueError('job_limit')
        return [self.load(p.stem) for p in paths]
    def refresh(self,s):
        now=time.time();change=False
        if s['state'] not in TERMINAL and s['deadline']<=now:
            s.update(state='expired',error={'code':'deadline'},lease=None,lease_until=None);change=True
        elif s['state']=='running' and s['lease_until']<=now:
            s.update(state='failed' if s['attempts']>=3 else 'pending',lease=None,lease_until=None)
            if s['state']=='failed':s['error']={'code':'lease_attempts_exhausted'}
            change=True
        if change:self.save(s)
        return s
    def load(self,jid):
        s=read(self.path(jid),1500000)
        try:
            if not isinstance(s,dict) or s['id']!=jid or s['owner']!=self.meta['owner'] or s['session']!=self.meta['session']:raise ValueError()
            if s['state'] not in ('pending','running','completed','failed','expired','cancelled') or s['delivery'] not in ('waiting','delivered','ambiguous'):raise ValueError()
            for k in ('created','deadline'):
                if type(s[k]) not in (int,float) or not math.isfinite(s[k]):raise ValueError()
            if not 0<s['deadline']-s['created']<=1800:raise ValueError()
            for k in ('attempts','lease_epoch'):
                if type(s[k]) is not int or not 0<=s[k]<=3:raise ValueError()
            if s['attempts']!=s['lease_epoch']:raise ValueError()
            if digest(s['request'])!=s['request_sha256']:raise ValueError()
            if s['state']=='running':
                checked(s['lease'])
                if type(s['lease_until']) not in (int,float) or not math.isfinite(s['lease_until']) or s['lease_until']>s['deadline']:raise ValueError()
            if 'completion_sha256' in s:
                answer={'result':s['result']} if 'result' in s else {'error':s['error']}
                if digest(answer)!=s['completion_sha256']:raise ValueError()
        except (KeyError,TypeError,ValueError,QueueError):raise QueueError('invalid_state_record') from None
        return self.refresh(s)
    @staticmethod
    def public(s,with_request=False):
        d={k:s[k] for k in ('id','owner','session','request_sha256','created','deadline','state','attempts','lease_epoch','delivery')}
        for k in ('lease','lease_until','error','result','completion_sha256'):
            if k in s and s[k] is not None:d[k]=s[k]
        if with_request:d['request']=s['request']
        return d
    def enqueue(self,request,*,owner,session,timeout=180):
        self.scope(owner,session);number(timeout,.05,1800)
        data=encode(request)
        if len(data)>protocol.MAX_REQUEST:raise QueueError('request_too_large')
        try:
            unknown=protocol.validate_request(request)
            if unknown:raise QueueError('unsupported_input')
        except protocol.BridgeError as e:raise QueueError(e.code) from None
        except (TypeError,ValueError,UnicodeError,RecursionError):raise QueueError('invalid_request') from None
        sha=hashlib.sha256(data).hexdigest()
        with self.locked():
            states=self.states()
            if any(s['request_sha256']==sha for s in states):raise QueueError('request_replay')
            if any(s['state'] not in TERMINAL or (s['state']=='completed' and s['delivery']=='waiting') for s in states):raise QueueError('request_inflight')
            if len(states)>=self.meta['max_jobs']:raise QueueError('job_limit')
            now=time.time();s={'id':uuid.uuid4().hex,'owner':owner,'session':session,'request':request,'request_sha256':sha,'created':now,'deadline':now+timeout,'state':'pending','attempts':0,'lease_epoch':0,'delivery':'waiting'}
            protocol.atomic_json(self.path(s['id']),s,exclusive=True);return self.public(s)
    def claim(self,*,owner,session,wait=0,lease_seconds=30):
        self.scope(owner,session);number(wait,0,20);number(lease_seconds,.05,240)
        end=time.monotonic()+wait
        while True:
            with self.locked():
                for s in self.states():
                    if s['state']=='pending':
                        if digest(s['request'])!=s['request_sha256']:raise QueueError('request_hash_mismatch')
                        s.update(state='running',lease=uuid.uuid4().hex,lease_until=min(time.time()+lease_seconds,s['deadline']),lease_epoch=s['lease_epoch']+1,attempts=s['attempts']+1)
                        self.save(s);return self.public(s,True)
            if time.monotonic()>=end:return None
            self.wake.wait(end-time.monotonic())
    def get(self,jid,*,owner,session):
        self.scope(owner,session)
        with self.locked():return self.public(self.load(jid))
    def _lease(self,s,lease,epoch,sha):
        checked(lease)
        if type(epoch) is not int or not 1<=epoch<=3 or not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha):raise QueueError('invalid_correlation')
        if s['state']!='running' or s.get('lease')!=lease or s['lease_epoch']!=epoch or s['request_sha256']!=sha:raise QueueError('stale_lease_or_correlation')
    def renew(self,jid,lease,epoch,sha,*,owner,session,seconds=30):
        self.scope(owner,session);number(seconds,.05,240)
        with self.locked():
            s=self.load(jid);self._lease(s,lease,epoch,sha)
            s['lease_until']=min(time.time()+seconds,s['deadline']);self.save(s);return self.public(s)
    def complete(self,jid,lease,epoch,sha,*,owner,session,result=None,error=None):
        self.scope(owner,session)
        if (result is None)==(error is None):raise QueueError('exactly_one_result_or_error')
        answer={'result':result} if result is not None else {'error':error}
        if len(encode(answer))>protocol.MAX_RESPONSE:raise QueueError('result_too_large')
        ah=digest(answer)
        checked(lease)
        if type(epoch) is not int or not 1<=epoch<=3 or not isinstance(sha,str) or not re.fullmatch(r'[0-9a-f]{64}',sha):raise QueueError('invalid_correlation')
        with self.locked():
            s=self.load(jid)
            if s['state'] in ('completed','failed') and s.get('completed_lease')==lease and s.get('completion_sha256')==ah and s['lease_epoch']==epoch and s['request_sha256']==sha:
                return {'id':jid,'state':s['state'],'idempotent':True}
            self._lease(s,lease,epoch,sha)
            if result is not None:
                try:protocol.validate_result(result,s['request'],jid)
                except protocol.BridgeError as e:raise QueueError(e.code) from None
                except (TypeError,ValueError,UnicodeError,RecursionError):raise QueueError('invalid_result') from None
            elif not isinstance(error,dict) or set(error)!={'code'} or not isinstance(error['code'],str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',error['code']):raise QueueError('invalid_error')
            s.update(state='completed' if result is not None else 'failed',completed_lease=lease,completion_sha256=ah,lease=None,lease_until=None,**answer)
            self.save(s);return {'id':jid,'state':s['state'],'idempotent':False}
    def cancel(self,jid,*,owner,session,reason='cancelled'):
        self.scope(owner,session)
        if reason not in ('cancelled','client_disconnected','producer_stopped'):raise QueueError('invalid_reason')
        with self.locked():
            s=self.load(jid)
            if s['state'] not in TERMINAL:s.update(state='cancelled',error={'code':reason},lease=None,lease_until=None);self.save(s)
            elif s['state']=='completed' and s['delivery']=='waiting':s['delivery']='ambiguous';self.save(s)
            return self.public(s)
    def delivery(self,jid,*,owner,session,value):
        self.scope(owner,session)
        if value not in ('delivered','ambiguous'):raise QueueError('invalid_delivery')
        with self.locked():
            s=self.load(jid)
            if s['state']!='completed' or s['delivery']!='waiting':raise QueueError('delivery_closed')
            s['delivery']=value;self.save(s)
    def recover_producer(self,*,owner,session):
        """Only holder of producer.lock may call; never redeliver uncertain completions."""
        self.scope(owner,session)
        with self.locked():
            for s in self.states():
                if s['state'] not in TERMINAL:s.update(state='cancelled',error={'code':'producer_stopped'},lease=None,lease_until=None);self.save(s)
                elif s['state']=='completed' and s['delivery']=='waiting':s['delivery']='ambiguous';self.save(s)


def request_for_text(text):
    return {'model':'native-subagent-bridge','stream':True,'input':[{'role':'user','content':[{'type':'input_text','text':text}]}]}

class Wake:
    """Linux inotify hint when available, bounded polling everywhere else."""
    def __init__(self,directory,poll=.01,notify=True):
        self.fd=-1;self.poll=poll;self.mode='poll'
        if notify:
            try:
                import ctypes
                libc=ctypes.CDLL(None,use_errno=True)
                fd=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
                if fd<0:raise OSError('inotify_init_failed')
                if libc.inotify_add_watch(fd,os.fsencode(directory),0x00000008|0x00000080|0x00000100|0x00000200)<0:
                    os.close(fd);raise OSError('inotify_watch_failed')
                self.fd=fd;self.mode='inotify+poll'
            except (AttributeError,OSError,ImportError):pass
    def wait(self,seconds=None):
        delay=self.poll if seconds is None else min(self.poll,max(0,seconds))
        if self.fd<0:time.sleep(delay);return
        if select.select([self.fd],[],[],delay)[0]:
            try:
                while os.read(self.fd,65536):pass
            except BlockingIOError:pass
    def close(self):
        if self.fd>=0:os.close(self.fd);self.fd=-1

class Producer:
    def __init__(self,q):
        self.q=q;self.fd=os.open(q.root/'producer.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:fcntl.flock(self.fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:os.close(self.fd);raise QueueError('producer_already_running') from None
        try:
            if not stat.S_ISREG(os.fstat(self.fd).st_mode):raise QueueError('invalid_producer_lock')
            q.recover_producer(owner=q.meta['owner'],session=q.meta['session'])
        except Exception:os.close(self.fd);self.fd=None;raise
    def close(self):
        if self.fd is not None:
            try:self.q.recover_producer(owner=self.q.meta['owner'],session=self.q.meta['session'])
            finally:os.close(self.fd);self.fd=None
