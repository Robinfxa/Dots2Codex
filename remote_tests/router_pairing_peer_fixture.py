"""Synthetic Mac-side bundle publication for the native pairing integration test.

No network, OAuth, admission, or inference. Real protocol validators are used.
"""
import json
import sys
import time
from pathlib import Path
from remote_transport import router_bootstrap as b
from remote_transport.control import initial_state, block_for
from remote_transport.model import canonical, deployment, require

value = json.loads(Path(sys.argv[1]).read_bytes())
handoff = json.loads(Path(value['handoff']).read_bytes())
state = b.decode_block(value['text'].strip() + '\n')
code = handoff['join_code']
if value.get('mode') == 'abort':
    print(json.dumps({'bootstrap': b.block_for(b.abort_bootstrap(state, join_code=code, reason='synthetic_stop'))}))
    raise SystemExit(0)
native = b.verify_worker_admission(state, code)
probe = state['worker']['probe']; actual = value['probe']
require(actual['id'] == probe['file_id'] and actual['name'] == probe['name'] and
        state['folder_id'] in actual['parents'], 'synthetic_reverse_probe_binding')
raw = Path(actual['path']).read_bytes()
require(raw == canonical({'contract': 'dots-router-probe/1', 'bootstrap_id': state['bootstrap_id'],
                          'native_task_id': native}), 'synthetic_reverse_probe_raw')
selection = state['required_selection']; admission = state['worker']['admission']
pin = deployment(state['session_id'], native, seconds=600, max_requests=128,
                 scope='responses_tools', inference={'selection': selection, 'admission': admission})
control = state['control']
config = {'document_id': control['document_id'], 'tab_id': control['tab_id'], 'control_id': control['control_id'],
          'writer_identity': control['worker_writer_identity'], 'folder_id': state['folder_id']}
ready = b.bundle_ready(state, join_code=code, pin_raw=pin.raw, config_raw=canonical(config), deployment_hash=pin.oid)
print(json.dumps({'bootstrap': b.block_for(ready), 'control': block_for(initial_state(pin, control['control_id']))}))
