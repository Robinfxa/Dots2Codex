"""Adversarial Drive simulator, never a live-service guarantee."""
from remote_transport.backend import Capabilities, AlreadyExists
from remote_transport.model import ProtocolError


class FakeDrive:
    capabilities = Capabilities(complete_listing=True, create_by_id=True, direct_metadata_read=True)

    def __init__(self):
        self.files = {}
        self.counter = 0
        self.hidden = set()
        self.page_size = 100
        self.fail_create_after_commit = False
        self.fail_list_page = None
        self.incomplete = False
        self.reverse = False
        self.fail_get = set()
        self.create_calls = []

    def generate_id(self):
        self.counter += 1
        return 'id_' + str(self.counter)

    def create_bytes(self, folder, name, raw, file_id):
        self.create_calls.append(file_id)
        file_id = file_id or self.generate_id()
        if file_id in self.files:
            raise AlreadyExists()
        self.files[file_id] = dict(raw=raw, name=name, folder=folder)
        if self.fail_create_after_commit:
            self.fail_create_after_commit = False
            raise ProtocolError('drive_network_outcome_unknown')
        return file_id

    def get_bytes(self, file_id, limit):
        if file_id in self.fail_get:
            raise ProtocolError('drive_http_403')
        return self.files[file_id]['raw']

    def list_page(self, folder, token, size):
        index = int(token or '0')
        if self.fail_list_page == index:
            raise ProtocolError('drive_http_429')
        ids = [i for i,f in self.files.items() if i not in self.hidden and f['folder'] == folder]
        if self.reverse:
            ids.reverse()
        size = min(size, self.page_size)
        out = {'files':[dict(id=i,name=self.files[i]['name'],parents=[folder],trashed=False)
                        for i in ids[index:index+size]], 'incompleteSearch':self.incomplete}
        if index+size < len(ids):
            out['nextPageToken'] = str(index+size)
        return out

    def get_metadata(self,file_id):
        value=self.files[file_id]
        return {'id':file_id,'name':value['name'],'parents':[value['folder']],'trashed':False}
