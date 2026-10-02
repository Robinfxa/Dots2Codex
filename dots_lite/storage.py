"""Exclusive, private, fsynced local evidence. No missing-journal resurrection.

Durability assumes a single cooperative host and intact non-rolled-back storage.
A file lock cannot fence another host holding a copied journal.
"""
import contextlib
import copy
import fcntl
import os
from pathlib import Path
import secrets
import stat
from .protocol import canonical, strict_json, require, sha256, ProtocolError, METADATA_MAX_BYTES


def fsync_dir(path):
    fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)

def private_read(path,cap=METADATA_MAX_BYTES):
    path=Path(path)
    try:
        fd=os.open(str(path),os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as handle:
            info=os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077,'unsafe_private_file')
            require(info.st_size<=cap,'local_file_too_large')
            data=handle.read(cap+1)
            require(len(data)<=cap,'local_file_too_large')
            return data
    except OSError:
        raise ProtocolError('private_file_missing_or_unsafe') from None

def private_write(path,data,immutable=False):
    path=Path(path);require(type(data) is bytes,'bytes_required')
    require(not path.is_symlink(),'unsafe_private_file')
    if immutable and path.exists():
        require(private_read(path,max(len(data),1))==data,'immutable_file_conflict')
        return
    tmp=path.parent/('.'+path.name+'.'+secrets.token_hex(8)+'.tmp')
    try:
        fd=os.open(str(tmp),os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as handle:
            handle.write(data);handle.flush();os.fsync(handle.fileno())
        if immutable:
            try:os.link(tmp,path)
            except FileExistsError:
                require(private_read(path,max(len(data),1))==data,'immutable_file_conflict')
            tmp.unlink()
        else:os.replace(tmp,path)
        fsync_dir(path.parent)
    finally:
        if tmp.exists():tmp.unlink()

class Journal:
    """One compact route journal, locked for the full read/modify/fsync operation."""
    def __init__(self,directory):
        self.directory=Path(directory)
        require(self.directory.is_dir() and not self.directory.is_symlink(),'journal_missing_no_execution')
        info=self.directory.stat()
        require(info.st_uid==os.getuid() and not info.st_mode&0o077,'unsafe_journal_directory')
        require((self.directory/'identity.json').is_file() and (self.directory/'journal.json').is_file(),'journal_missing_no_execution')
        self.identity=strict_json(private_read(self.directory/'identity.json',4096))
        require(type(self.identity) is dict and set(self.identity)=={'journal_id','contract'},'journal_identity_corrupt')
        require(self.identity['contract']=='dots-lite-local/3','journal_version_mismatch')

    @classmethod
    def create(cls,directory,state):
        directory=Path(directory)
        require(not directory.exists() and not directory.is_symlink(),'journal_already_exists')
        directory.mkdir(mode=0o700,parents=False)
        identity={'journal_id':secrets.token_hex(32),'contract':'dots-lite-local/3'}
        private_write(directory/'identity.json',canonical(identity),immutable=True)
        state=copy.deepcopy(state);state['journal_id']=identity['journal_id'];state['generation']=0
        payload={'state':state,'sha256':sha256(canonical(state))}
        private_write(directory/'journal.json',canonical(payload),immutable=True)
        fd=os.open(str(directory/'actor.lock'),os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd);fsync_dir(directory)
        return cls(directory)

    @contextlib.contextmanager
    def locked(self):
        fd=os.open(str(self.directory/'actor.lock'),os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode),'unsafe_actor_lock')
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ProtocolError('actor_busy') from None
            yield self
        finally:
            fcntl.flock(fd,fcntl.LOCK_UN);os.close(fd)

    def read(self):
        value=strict_json(private_read(self.directory/'journal.json'))
        require(type(value) is dict and set(value)=={'state','sha256'} and type(value['state']) is dict,'journal_corrupt_no_execution')
        state=value['state']
        require(value['sha256']==sha256(canonical(state)) and state.get('journal_id')==self.identity['journal_id'],'journal_corrupt_no_execution')
        require(type(state.get('generation')) is int and state['generation']>=0,'journal_corrupt_no_execution')
        return state

    def write(self,state):
        require(state.get('journal_id')==self.identity['journal_id'],'journal_identity_mismatch')
        state=copy.deepcopy(state);state['generation']+=1
        payload=canonical({'state':state,'sha256':sha256(canonical(state))})
        # Three maximum-size route journals plus activation metadata fit 4 MiB.
        require(len(payload)<=1024*1024,'active_metadata_cap_reached')
        private_write(self.directory/'journal.json',payload)
        return state

    def snapshot(self):
        with self.locked():return copy.deepcopy(self.read())

def burn_fence(directory,name,evidence):
    """Irreversible bounded marker independent of mutable journal replacement.

    This catches journal-only rollback while the marker remains. It cannot
    detect a rollback of the entire host/storage volume, which is outside the
    exclusive intact-storage assumption. Marker creation uncertainty burns it.
    """
    require(name and '/' not in name and '\\' not in name and name not in {'.','..'},'invalid_fence_name')
    path=Path(directory)/name;raw=canonical(evidence)
    require(len(raw)<=8192,'fence_evidence_too_large')
    try:fd=os.open(str(path),os.O_CREAT|os.O_EXCL|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
    except FileExistsError:raise ProtocolError('fence_already_burned_no_retry') from None
    with os.fdopen(fd,'wb') as handle:
        handle.write(raw);handle.flush();os.fsync(handle.fileno())
    fsync_dir(directory)
