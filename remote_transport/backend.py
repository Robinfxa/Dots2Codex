"""Append-only object transport ports. Listing is observation, never ownership."""
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from .model import Object, ProtocolError, MAX_BYTES, require

MAX_OBJECTS = 128
MAX_PAGES = 8


@dataclass(frozen=True)
class Capabilities:
    complete_listing: bool
    create_by_id: bool = False
    direct_metadata_read: bool = False


class AlreadyExists(Exception):
    pass


class TransportBackend(Protocol):
    def reserve(self): ...
    def publish(self, obj: Object, reservation): ...
    def scan(self, deployment_id: str): ...
    def reference(self, obj, publication): ...
    def fetch(self, reference): ...


def filename(obj):
    return 'ddv0-' + obj.body['identity']['deployment_id'] + '-' + obj.oid + '.json'


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_private_file(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                not info.st_mode & 0o077 and info.st_size <= limit, 'unsafe_local_file')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            raw = f.read(limit + 1)
        require(len(raw) <= limit, 'local_file_too_large')
        return raw
    finally:
        os.close(fd)


class LocalFSBackend:
    """Object protocol conformance backend; NOT the legacy queue or Drive sync."""
    def __init__(self, root, *, create=False):
        self.root = Path(root)
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        require(not self.root.is_symlink(), 'unsafe_local_directory')
        st = self.root.stat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077,
                'unsafe_local_directory')

    def reserve(self):
        return None

    def publish(self, obj, reservation=None):
        obj.validate()
        path = self.root / filename(obj)
        tmp = self.root / ('.tmp-' + uuid.uuid4().hex)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as out:
                out.write(obj.raw)
                out.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        try:
            try:
                # Atomic create-if-absent exposes only the fully fsynced bytes.
                os.link(tmp, path, follow_symlinks=False)
            except FileExistsError:
                require(read_private_file(path, MAX_BYTES) == obj.raw, 'immutable_object_conflict')
            fsync_dir(self.root)
        finally:
            tmp.unlink()
            fsync_dir(self.root)
        return path.name

    def scan(self, deployment_id):
        found = []
        for path in sorted(self.root.glob('ddv0-' + deployment_id + '-*.json')):
            require(len(found) < MAX_OBJECTS, 'object_budget_exceeded')
            obj = Object.parse(read_private_file(path, MAX_BYTES))
            require(path.name == filename(obj), 'object_name_mismatch')
            found.append(obj)
        return found

    def reference(self,obj,publication):
        require(publication==filename(obj),'publication_locator_mismatch')
        return {'object_id':obj.oid,'locator':{'backend':'localfs','name':publication}}

    def fetch(self,reference):
        validate_reference(reference)
        locator=reference['locator']
        require(locator['backend']=='localfs','message_backend_mismatch')
        obj=Object.parse(read_private_file(self.root/locator['name'],MAX_BYTES))
        require(obj.oid==reference['object_id'] and filename(obj)==locator['name'],'message_reference_mismatch')
        return obj


class GoogleDriveBackend:
    """Injected authenticated API client, no credential discovery or model relay.

    client must expose capabilities and generate_id/create_bytes/get_bytes/list_page.
    list_page returns {files:[{id,name,parents,trashed}], nextPageToken?, incompleteSearch?}.
    No automatic call retries occur here. Session recovery fixes bytes and reservation.
    """
    def __init__(self, client, folder_id, *, mode='strict_ids', discovery='list'):
        require(isinstance(folder_id, str) and re.fullmatch('[A-Za-z0-9_-]{1,256}', folder_id),
                'invalid_folder_id')
        require(mode in {'strict_ids', 'duplicate_tolerant'}, 'invalid_drive_mode')
        require(discovery in {'list','control_refs'},'invalid_discovery_mode')
        if discovery=='list':
            require(client.capabilities.complete_listing, 'complete_listing_capability_required')
        else:
            require(client.capabilities.direct_metadata_read,'direct_metadata_capability_required')
        if mode == 'strict_ids':
            require(client.capabilities.create_by_id, 'create_by_id_capability_required')
        self.client, self.folder_id, self.mode, self.discovery = client, folder_id, mode, discovery

    def reserve(self):
        if self.mode == 'strict_ids':
            value = self.client.generate_id()
            require(isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,256}', value),
                    'invalid_generated_id')
            return value
        return None

    def publish(self, obj, reservation):
        obj.validate()
        require((self.mode == 'strict_ids') == (reservation is not None), 'reservation_mismatch')
        try:
            remote_id = self.client.create_bytes(self.folder_id, filename(obj), obj.raw, reservation)
        except AlreadyExists:
            require(reservation is not None, 'unexpected_duplicate_response')
            remote_id = reservation
        require(isinstance(remote_id, str) and remote_id, 'invalid_create_response')
        if reservation is not None:
            require(remote_id == reservation, 'create_returned_wrong_id')
        # A successful HTTP create alone is not verified immutable publication.
        require(self.client.get_bytes(remote_id, MAX_BYTES) == obj.raw, 'readback_mismatch')
        return remote_id

    def scan(self, deployment_id):
        require(self.discovery=='list','listing_disabled_for_control_refs')
        token, seen_tokens, objects, physical_ids = None, set(), [], set()
        prefix = 'ddv0-' + deployment_id + '-'
        for _ in range(MAX_PAGES):
            page = self.client.list_page(self.folder_id, token, 100)
            require(isinstance(page, dict) and type(page.get('files')) is list,
                    'malformed_list_page')
            require(type(page.get('incompleteSearch',False)) is bool, 'malformed_list_page')
            require(page.get('incompleteSearch') is not True, 'incomplete_search')
            for item in page['files']:
                require(isinstance(item, dict) and isinstance(item.get('id'), str) and
                        isinstance(item.get('name'), str) and type(item.get('parents')) is list,
                        'malformed_file_metadata')
                require(self.folder_id in item['parents'] and item.get('trashed') is False,
                        'unexpected_file_scope')
                if not item['name'].startswith(prefix):
                    continue
                require(item['id'] not in physical_ids, 'duplicate_physical_listing')
                physical_ids.add(item['id'])
                require(len(physical_ids) <= MAX_OBJECTS, 'object_budget_exceeded')
                obj = Object.parse(self.client.get_bytes(item['id'], MAX_BYTES))
                require(item['name'] == filename(obj) and
                        obj.body['identity']['deployment_id'] == deployment_id, 'object_name_mismatch')
                objects.append(obj)
            token = page.get('nextPageToken')
            if token is None:
                return objects
            require(isinstance(token, str) and token and token not in seen_tokens, 'invalid_page_token')
            seen_tokens.add(token)
        raise ProtocolError('page_budget_exceeded')

    def reference(self,obj,publication):
        require(isinstance(publication,str) and publication!='observed_exact_bytes' and
                re.fullmatch('[A-Za-z0-9_-]{1,256}',publication),'verified_physical_id_required')
        return {'object_id':obj.oid,'locator':{'backend':'drive','folder_id':self.folder_id,'file_id':publication}}

    def fetch(self,reference):
        validate_reference(reference)
        locator=reference['locator']
        require(locator['backend']=='drive' and locator['folder_id']==self.folder_id,'message_backend_or_folder_mismatch')
        require(self.client.capabilities.direct_metadata_read,'direct_metadata_capability_required')
        meta=self.client.get_metadata(locator['file_id'])
        require(isinstance(meta,dict) and meta.get('id')==locator['file_id'] and meta.get('trashed') is False and
                type(meta.get('parents')) is list and all(isinstance(parent,str) for parent in meta['parents']) and
                self.folder_id in meta['parents'],'message_metadata_scope_mismatch')
        obj=Object.parse(self.client.get_bytes(locator['file_id'],MAX_BYTES))
        require(obj.oid==reference['object_id'] and meta.get('name')==filename(obj),'message_reference_mismatch')
        return obj


def validate_reference(reference):
    from .model import valid_hash
    require(isinstance(reference,dict) and set(reference)=={'object_id','locator'} and
            valid_hash(reference['object_id']) and isinstance(reference['locator'],dict),'invalid_message_reference')
    locator=reference['locator']
    if locator.get('backend')=='drive':
        require(set(locator)=={'backend','folder_id','file_id'} and all(isinstance(locator[k],str) and
                re.fullmatch('[A-Za-z0-9_-]{1,256}',locator[k]) for k in ('folder_id','file_id')),'invalid_message_locator')
    elif locator.get('backend')=='localfs':
        require(set(locator)=={'backend','name'} and isinstance(locator['name'],str) and
                re.fullmatch(r'ddv0-[A-Za-z0-9_:/.-]{1,256}-[0-9a-f]{64}\.json',locator['name']) and
                '/' not in locator['name'] and '..' not in locator['name'],'invalid_message_locator')
    else:raise ProtocolError('unknown_message_locator')
    return reference
