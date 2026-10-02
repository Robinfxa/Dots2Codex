"""Offline fixture for startup_benchmark.js; never constructs a real provider.

This helper is executed with the repository under comparison as its working
directory. It imports THAT repository's production code and synthetic provider,
so a baseline can be compared without modifying or copying its source tree.
All resource identifiers and pairing material are synthetic and stay private.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import time


def prepare(root, scenario, minimum_state_bytes=0):
    if type(minimum_state_bytes) is not int or not 0 <= minimum_state_bytes <= 128 * 1024:
        raise ValueError("minimum state bytes must be between 0 and 131072")
    if scenario == "join" and minimum_state_bytes:
        raise ValueError("a first-JOIN fixture cannot have preexisting event history")
    sys.path.insert(0, str(Path.cwd()))
    from remote_tests.test_global_control import FakeGoogle
    from remote_transport import global_control as queue
    from remote_transport.global_gateway import Store, private_write
    from remote_transport.global_google import GoogleQueueBridge
    from remote_transport.global_native import NativeLedger
    from remote_transport.global_fixture import identity
    from remote_transport.model import canonical
    from remote_transport.router_join import _source_hashes
    from remote_transport.selection import select, load_catalog

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    selection = select(load_catalog(), 'gpt-6.1-sol', 'high')
    store, activation = Store.initialize(root / 'gateway', selection)
    provider = FakeGoogle()
    document = provider.create_document_once('synthetic-folder', 'synthetic queue')
    code = secrets.token_hex(32)
    now = int(time.time())
    due = scenario == 'claim_begin_due'
    initial = queue.initial(activation_id=activation, queue_id=secrets.token_hex(16),
        folder_id='synthetic-folder', document_id=document, tab_id='t.0',
        join_code=code, created=now - (800 if minimum_state_bytes else 40 if due else 5), expires=now + 1800,
        runtime_source_hashes=_source_hashes())
    bridge = GoogleQueueBridge(store, root / 'bridge', provider, provider, initial, code)
    bridge.initialize_blank_queue()
    native_id = '/root/offline_startup_controller'
    ledger = NativeLedger(root / 'native', initial, code, native_id)
    route = None
    serial = 0

    def event(kind, **args):
        nonlocal serial
        serial += 1
        path = root / 'native' / ('seed-%d.json' % serial)
        plan = ledger.plan_event(bridge.read(), kind, path, **args)
        response = provider.batch_update_document(**plan['tool_arguments'])
        ledger.verify_plan(path, response, provider.get_document(document))

    try:
        if scenario != 'join':
            if minimum_state_bytes:
                # Offline authentic history, not unsigned padding or expired
                # native reservations. Measured operations begin after this
                # history and retain their actual dispatch/acceptance windows.
                state = queue.transition(initial, code, 'join', 'native',
                    {'native_task_id': native_id, 'controller_epoch': secrets.token_hex(16),
                     'lease_expires': now + 1200, 'capacity': 2}, now=now-799)
                controller = state['logical']['controller']
                arguments = {key: controller[key] for key in ('native_task_id', 'controller_epoch')}
                tick = now-798
                while len(queue.block(state).encode()) < minimum_state_bytes:
                    if tick >= now-31:
                        raise ValueError('minimum size exceeds the bounded synthetic history')
                    state = queue.transition(state, code, 'heartbeat', 'native', arguments, now=tick)
                    tick += 1
                state = queue.transition(state, code, 'heartbeat', 'native', arguments,
                                         now=now-(30 if due else 2))
                queue.verify(state, code)
                provider.docs[document] = [queue.block(state), provider.docs[document][1] + 1]
            else:
                event('join', capacity=2, seconds=1200, now=now - (31 if due else 3))
                event('heartbeat', now=now - (30 if due else 2))
            bridge.sync_heartbeat()
            row = store.admission(activation, identity(), selection)
            route = row['id']
            bridge.prepare_child(route)
        if scenario == 'admission':
            event('claim', route_id=route)
            event('begin', route_id=route)
            native_plan = root / 'native' / 'prepared-native.json'
            planned = ledger.plan_spawn(bridge.read(), route, native_plan, Path.cwd())
            arguments = root / 'native' / 'actual-arguments.json'
            result = root / 'native' / 'native-result.json'
            private_write(arguments, canonical(planned['arguments']))
            private_write(result, canonical({'task_name': '/root/offline/' + planned['arguments']['task_name']}))
        private_write(root / 'join.txt', code.encode())
        private_write(root / 'document.json', canonical(provider.get_document(document)))
        metadata = {'cwd': str(Path.cwd()), 'stateDir': str(root / 'native'),
            'nativeTaskId': native_id, 'documentId': document, 'tabId': 't.0',
            'joinCodeFile': str(root / 'join.txt'), 'routeId': route,
            'initialEpoch': bridge.read().state['epoch'],
            'initialStateBytes': len(queue.block(bridge.read().state).encode()),
            'ledgerFile': str(ledger.path)}
        if scenario == 'admission':
            metadata.update(planFile=str(native_plan), actualArgumentsFile=str(arguments),
                            nativeResultFile=str(result))
        private_write(root / 'fixture.json', canonical(metadata))
    finally:
        bridge.close()


def audit(root, scenario):
    """Independently authenticate the measured result; emit aggregates only.

    This read-only audit is outside the measured adapter invocation. It never
    calls a provider, accepts a pending operation, or mints a native plan.
    """
    sys.path.insert(0, str(Path.cwd()))
    from remote_transport import global_control as queue
    from remote_transport.model import canonical, hash_bytes
    metadata = json.loads((root / 'fixture.json').read_text())
    document = json.loads((root / 'final-document.json').read_text())
    code = (root / 'join.txt').read_text()
    state = queue.snapshot(document, metadata['documentId'], metadata['tabId']).state
    queue.verify(state, code, require_fresh=False)
    ledger = json.loads(Path(metadata['ledgerFile']).read_text())
    events = state['events'][metadata['initialEpoch']:]
    ids = {event['operation_id'] for event in events}
    relevant = {'join': {'join', 'heartbeat', 'join-heartbeat'}, 'claim_begin': {'claim', 'begin', 'claim-begin'},
                'claim_prepare_native': {'claim', 'begin', 'claim-begin'},
                'claim_begin_due': {'heartbeat', 'claim', 'begin', 'claim-begin', 'heartbeat-claim-begin'},
                'admission': {'heartbeat', 'admitted', 'heartbeat-admitted'}}[scenario]
    # Include attempted but uncommitted records too, so a conflict cannot hide
    # a burned reservation just because the signed queue did not advance.
    records = [record for record in ledger['operations'].values()
               if (record.get('operation_id') in ids or ids.intersection(record.get('operation_ids', [])) or
                   (record['kind'] in relevant and 'cell-' in Path(record['path']).name))]
    paths = {record['path'] for record in records}
    verified_kinds = []
    exact_group = False
    group_pairs = {'claim-begin': ['claim', 'begin'], 'join-heartbeat': ['join', 'heartbeat'],
                   'heartbeat-admitted': ['heartbeat', 'admitted'],
                   'heartbeat-claim-begin': ['heartbeat', 'claim', 'begin']}
    for record in records:
        if record['kind'] not in group_pairs:
            if record['status'] == 'verified':
                verified_kinds.append(record['kind'])
            continue
        packet = json.loads(Path(record['path']).read_text())
        assert record['sha256'] == hash_bytes(canonical(packet))
        assert packet['contract'] == 'dots-global-cas-group-plan/1'
        count = len(group_pairs[record['kind']])
        assert record['operation_ids'] == [event['operation_id'] for event in packet['expected_state']['events'][-count:]]
        assert [event['kind'] for event in packet['expected_state']['events'][-count:]] == group_pairs[record['kind']]
        assert packet['group_kind'] == record['kind']
        if record['status'] == 'verified':
            expected = packet['expected_state']
            assert state['events'][:expected['epoch']] == expected['events']
            verified_kinds.extend(group_pairs[record['kind']])
            exact_group = len(records) == 1 and len(paths) == 1
    return {'authenticated': True, 'new_event_kinds': [event['kind'] for event in events],
            'verified_operation_kinds': sorted(verified_kinds),
            'unresolved_operation_count': sum(record['status'] != 'verified' for record in records),
            'reservation_count': len(records), 'distinct_reserved_plan_count': len(paths),
            'claim_begin_shared_plan': exact_group and scenario == 'claim_begin',
            'heartbeat_claim_begin_shared_plan': exact_group and scenario == 'claim_begin_due',
            'join_heartbeat_shared_plan': exact_group and scenario == 'join',
            'heartbeat_admitted_shared_plan': exact_group and scenario == 'admission',
            'shared_plan': exact_group,
            'child_imported': bool(metadata['routeId'] and ledger['spawns'].get(metadata['routeId'], {}).get('child_admission_import', {}).get('status') == 'imported'),
            'route_state': state['logical']['demands'][metadata['routeId']]['state']
                           if metadata['routeId'] else None,
            'native_spawn_reservations': len(ledger['spawns'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--scenario', choices=['join', 'claim_begin', 'claim_begin_due', 'claim_prepare_native', 'admission'], required=True)
    parser.add_argument('--audit-final', action='store_true')
    parser.add_argument('--minimum-state-bytes', type=int, default=0)
    args = parser.parse_args()
    os.umask(0o077)
    if args.audit_final:
        print(json.dumps(audit(args.root, args.scenario)))
    else:
        prepare(args.root, args.scenario, args.minimum_state_bytes)
