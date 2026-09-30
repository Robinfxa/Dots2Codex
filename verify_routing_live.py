"""Read-only verification of routed live artifacts; GUI/native-tool evidence remains separate."""
import argparse
import hashlib
import json
from pathlib import Path

from portable import Deployment, FileQueue, QueueError, digest, read
from routing import Registry
from routing_desktop import verify_freeze
from tool_probe import expected_final


class SnapshotQueue(FileQueue):
    def refresh(self, state):
        # FileQueue.load still validates identities/hashes/types, but never writes expiry transitions.
        return state


def verify(root, minimum_requests=2, require_cleanup=False, expected_sessions=('a', 'b')):
    freeze = verify_freeze()
    reg = Registry(root); state = reg.snapshot(); reports = []
    if not expected_sessions or set(state['sessions']) != set(expected_sessions):
        raise QueueError('live_expected_sessions_mismatch')
    if type(minimum_requests) is not int or not 1 <= minimum_requests <= 3:
        raise QueueError('invalid_live_request_minimum')
    confirmed = [a for a in state['admissions'].values() if a['state'] == 'confirmed']
    for field in ('native_task_id', 'worker_instance', 'assignment_id'):
        if len({a[field] for a in confirmed}) != len(confirmed):
            raise QueueError('live_native_context_not_unique')
    for key, route in state['sessions'].items():
        d = Deployment(reg.root / 'sessions' / key)
        q = SnapshotQueue(d.queue_root)
        try:
            with q.locked(): jobs = q.states()
        finally: q.close()
        jobs.sort(key=lambda job: job['created'])
        control = read(d.root / 'control/state.json', 65536)
        start = read(d.root / 'evidence/sticky-desktop-start.json', 32768)
        if start['source_sha256'] != freeze['aggregate_sha256']:
            raise QueueError('live_source_freeze_mismatch')
        if len(jobs) < minimum_requests:
            raise QueueError('live_requests_incomplete')
        if any(job['state'] != 'completed' or job['delivery'] != 'delivered' for job in jobs):
            raise QueueError('live_request_not_completed_and_delivered')
        admissions = [a for a in state['admissions'].values() if a['session_key'] == key]
        assignments = {a['assignment_id']: a for a in admissions if a['state'] == 'confirmed'}
        generations = []
        for job in jobs:
            dispatch = control['dispatch'][job['id']]
            admission = assignments.get(dispatch['assignment_id'])
            if (not admission or dispatch['status'] != 'completed' or
                    dispatch['epoch'] != admission['broker_epoch'] or
                    dispatch['lease_epoch'] != job['lease_epoch'] or
                    dispatch['request_sha256'] != job['request_sha256']):
                raise QueueError('live_dispatch_binding_mismatch')
            generations.append(admission['generation'])
        if generations[:2] != [1, 1]:
            raise QueueError('initial_turns_not_same_worker')
        if route['scope'] == 'text_only':
            suffixes = ['FIRST', 'SECOND', 'THIRD']
            for job, suffix in zip(jobs, suffixes):
                if job['result'] != dict(kind='message', text=start['marker'] + ':' + suffix):
                    raise QueueError('live_text_session_marker_mismatch')
            if len(jobs) == 3 and generations != [1, 1, 2]:
                raise QueueError('live_replacement_generation_not_proven')
            scope_check = 'per-session marker matched'
        else:
            if len(jobs) != 2 or jobs[0]['result'].get('kind') != 'function_call':
                raise QueueError('live_tool_request_count_mismatch')
            reservation = control['tool_probe']
            if reservation['job_id'] != jobs[0]['id'] or reservation['intent_sha256'] != digest(jobs[0]['result']):
                raise QueueError('live_tool_reservation_mismatch')
            expected = expected_final(jobs[1]['request'], reservation)
            if jobs[1]['result'] != dict(kind='message', text=expected):
                raise QueueError('live_tool_actual_output_mismatch')
            scope_check = 'one strict tool intent and correlated actual-output final'
        outcome_path = d.root / 'evidence/desktop-outcome.json'
        cleaned = False
        if outcome_path.exists():
            outcome = read(outcome_path, 8192)
            cleaned = outcome['returncode'] == 0 and outcome['service_stopped'] is True
        if require_cleanup and not cleaned:
            raise QueueError('live_cleanup_not_verified')
        reports.append(dict(session=key, scope=route['scope'], jobs=len(jobs), generations=generations,
                            worker_instances=len(assignments), results=scope_check, cleanup_verified=cleaned))
    return dict(contract=state['contract'], source_sha256=freeze['aggregate_sha256'],
                sessions=reports, admissions=len(state['admissions']),
                gui_pixels_verified=False, native_platform_identity_independently_verified=False,
                note='Read-only file checks. Parent separately verifies actual GUI, actual native task admission and cleanup.')


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--registry', required=True)
    p.add_argument('--minimum-requests', type=int, default=2); p.add_argument('--require-cleanup', action='store_true')
    p.add_argument('--expected-sessions', nargs='+', default=['a', 'b'])
    a = p.parse_args()
    print(json.dumps(verify(a.registry, a.minimum_requests, a.require_cleanup, tuple(a.expected_sessions)), indent=2))


if __name__ == '__main__': main()
