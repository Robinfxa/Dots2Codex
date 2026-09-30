"""Read-only evidence extraction; never substitutes for actual GUI/native-task observation."""
import argparse
import json
import re
from pathlib import Path

from portable import Deployment, QueueError, digest, read
from routing import Registry
from routing_desktop import verify_freeze
from verify_routing_live import SnapshotQueue
from repo_review import receipt


def verify(registry, session='review', require_cleanup=False):
    freeze = verify_freeze()
    reg = Registry(registry); state = reg.snapshot(); route = reg.session(state, session)
    if route['scope'] != 'repo_review':
        raise QueueError('repo_review_scope_required')
    admissions = [a for a in state['admissions'].values() if a['session_key'] == session]
    if len(admissions) != 1 or admissions[0]['state'] != 'confirmed' or route['generation'] != 1:
        raise QueueError('repo_review_single_native_worker_not_proven')
    d = Deployment(reg.root / 'sessions' / session)
    q = SnapshotQueue(d.queue_root)
    try:
        with q.locked(): jobs = q.states()
    finally:
        q.close()
    jobs.sort(key=lambda j: j['created'])
    control = read(d.root / 'control/state.json', 65536)
    review = control.get('repo_review')
    if not review or not review.get('final'):
        raise QueueError('repo_review_not_final')
    start = read(d.root / 'evidence/sticky-desktop-start.json', 32768)
    if start['source_sha256'] != freeze['aggregate_sha256']:
        raise QueueError('live_source_freeze_mismatch')
    if any(j['state'] != 'completed' or j['delivery'] != 'delivered' for j in jobs):
        raise QueueError('repo_review_not_all_delivered')
    if len(jobs) != len(review['calls']) + 1:
        raise QueueError('repo_review_unexpected_job_count')
    for job in jobs:
        dispatch = control['dispatch'][job['id']]
        if dispatch['assignment_id'] != admissions[0]['assignment_id'] or dispatch['epoch'] != admissions[0]['broker_epoch'] or dispatch['status'] != 'completed' or dispatch['request_sha256'] != job['request_sha256']:
            raise QueueError('repo_review_sticky_binding_mismatch')
    final = next((j for j in jobs if j['id'] == review['final']['job_id']), None)
    if final is None or digest(final['result']) != review['final']['result_sha256'] or final['result'].get('kind') != 'message':
        raise QueueError('repo_review_final_mismatch')
    calls = [x for x in final['request']['input'] if x.get('type') == 'function_call']
    outputs = [x for x in final['request']['input'] if x.get('type') == 'function_call_output']
    if len(calls) != len(review['calls']) or len(outputs) != len(calls):
        raise QueueError('repo_review_receipts_incomplete')
    evidence = []
    for r, call, out in zip(review['calls'], calls, outputs):
        source = next((j for j in jobs if j['id'] == r['job_id']), None)
        if source is None or digest(source['result']) != r['intent_sha256'] or call.get('call_id') != r['call_id'] or out.get('call_id') != r['call_id'] or digest(out.get('output')) != r.get('output_sha256'):
            raise QueueError('repo_review_receipt_mismatch')
        observed = receipt(out.get('output'), r['action'])
        text = out['output'] if isinstance(out['output'], str) else '\n'.join(x['text'] for x in out['output'])
        duration = re.search(r'Wall time: ([0-9]+(?:\.[0-9]+)?) seconds', text)
        evidence.append(dict(tool='exec_command', action=r['action'], requested_at=source['created'],
                             wall_seconds=float(duration[1]) if duration else None, ok=observed.get('ok'),
                             error=observed.get('error'), source_url=observed.get('url'),
                             file_sha256=observed.get('file_sha256'), truncated=observed.get('truncated')))
    cleaned = False
    if (d.root / 'evidence/desktop-outcome.json').exists():
        outcome = read(d.root / 'evidence/desktop-outcome.json', 8192)
        cleaned = outcome['returncode'] == 0 and outcome['service_stopped'] is True
    if require_cleanup and not cleaned:
        raise QueueError('repo_review_cleanup_not_verified')
    return dict(scope='repo_review', repository='https://github.com/Robinfxa/Dots2Codex', outcome=review['outcome'],
                source_sha256=freeze['aggregate_sha256'], model_requests=len(jobs), tool_calls=len(evidence),
                same_worker=True, generations=[1], admissions=1, tool_evidence=evidence,
                final_text=final['result']['text'], cleanup_verified=cleaned,
                gui_pixels_verified=False, native_identity_requires_parent_platform_confirmation=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--registry',required=True);p.add_argument('--session',default='review');p.add_argument('--require-cleanup',action='store_true')
    args=p.parse_args();print(json.dumps(verify(args.registry,args.session,args.require_cleanup),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
