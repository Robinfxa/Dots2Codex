"""Synthetic gateway RPC/overlap benchmark with explicitly injected real sleeps.

All ports use the repository's in-memory FakeGoogle. Public results contain
aggregate counts/timings only. No provider requests or native dispatch occur.
"""
import argparse
from collections import Counter
import inspect
import json
from pathlib import Path
import sys
import threading
import time


def run_scenario(scenario, *, parallel=False, desktop=False, injected_ms=20):
    # Running this file against a baseline must import the baseline's modules.
    sys.path.insert(0, str(Path.cwd()))
    from remote_tests import test_global_heartbeat as fixtures
    from remote_transport.global_google import GoogleQueueBridge
    if scenario in ('publish_bundle', 'consume_and_ready'):
        from remote_tests.test_global_preparation_fast import PreparationFastTests
        f = PreparationFastTests('test_event_observes_exact_readback_with_two_gets_and_one_write')
    else:
        f = fixtures.GlobalHeartbeatTests('test_exact_900_root_policy_and_no_old_protocol_migration')
    f.setUp()
    counts = Counter()
    lock = threading.RLock()
    active = Counter()
    maximum = Counter()
    events = []
    delay_events = []
    owner = threading.get_ident()
    docs_owner_checks = []
    try:
        if scenario in ('publish_bundle', 'consume_and_ready'):
            route, state = f.admitted_child()
            if scenario == 'consume_and_ready':
                from remote_transport import global_control as queue, router_bootstrap as child
                f.bridge.advance_child(route)
                code = queue.child_code(f.code, f.generation, route)
                did = state['bootstrap_document_id']
                source = child.snapshot_from_document(f.google.get_document(did), did, state['bootstrap_tab_id'])
                polling = child.worker_polling(source.state, join_code=code,
                    native_task_id=source.state['worker']['native_task_id'], runtime_hash='a'*64)
                f.google.batch_update_document(**child.plan(source, polling, join_code=code)['tool_arguments'])
        else:
            f.join(latency=0)
        if parallel:
            assert 'parallel_preparation' in inspect.signature(GoogleQueueBridge).parameters
            f.bridge.parallel_preparation = True

        class Port:
            def __init__(self, name):
                self.name = name

            def __getattr__(self, method):
                original = getattr(f.google, method)
                if not callable(original):
                    return original

                def call(*args, **kwargs):
                    with lock:
                        counts[self.name + '.' + method] += 1
                        active[self.name] += 1
                        active['all'] += 1
                        maximum[self.name] = max(maximum[self.name], active[self.name])
                        maximum['all'] = max(maximum['all'], active['all'])
                        start = time.perf_counter()
                        if self.name == 'docs':
                            docs_owner_checks.append(threading.get_ident() == owner)
                    try:
                        # The actual wrapper supplies these callbacks/deadlines;
                        # our in-memory provider does not have a retry policy.
                        check = kwargs.pop('check', None)
                        kwargs.pop('deadline', None)
                        if check:
                            check()
                        delay_start = time.perf_counter()
                        time.sleep(injected_ms / 1000)
                        delay_end = time.perf_counter()
                        with lock:
                            delay_events.append((delay_start, delay_end))
                        if check:
                            check()
                        return original(*args, **kwargs)
                    finally:
                        with lock:
                            events.append((start, time.perf_counter()))
                            active[self.name] -= 1
                            active['all'] -= 1
                return call

        f.bridge.docs, f.bridge.drive = Port('docs'), Port('drive')
        if desktop:
            from remote_transport.global_desktop import RecoveringDocs
            f.bridge.docs = RecoveringDocs(f.bridge.docs, store=f.store, runtime=f.root,
                spec={'generation': getattr(f, 'gen', None) or f.generation, 'expires': f.initial['expires']},
                on_pause=lambda **kwargs: None, on_resume=lambda: None)
        start = time.perf_counter()
        if scenario == 'prepare_child':
            route = f.demand()
            assert route is not None
        elif scenario == 'idle_step':
            assert f.bridge.step()['state'] == 'controller_active'
        elif scenario == 'queue_event':
            f.bridge.event('close', {'confirm': True})
        elif scenario == 'publish_bundle':
            assert f.bridge.advance_child(route)['state'] == 'await_child_polling'
        elif scenario == 'consume_and_ready':
            assert f.bridge.advance_child(route)['state'] == 'ready'
        else:
            raise ValueError('unsupported synthetic scenario')
        elapsed = (time.perf_counter() - start) * 1000
        # Union of injected provider intervals, retaining concurrency overlap.
        def union(intervals):
            merged = []
            for left, right in sorted(intervals):
                if merged and left <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                else:
                    merged.append((left, right))
            return sum(b-a for a,b in merged)
        return {'scenario': scenario, 'parallel_preparation': parallel, 'desktop_recovery_wrapper': desktop,
                'provider_rpc_calls': sum(counts.values()), 'counts': dict(sorted(counts.items())),
                'injected_ms_per_provider_rpc': injected_ms,
                'sum_injected_rpc_delay_ms': sum(counts.values()) * injected_ms,
                'measured_synthetic_wall_ms': round(elapsed, 3),
                'measured_provider_interval_union_ms': round(union(events)*1000, 3),
                'measured_provider_interval_sum_ms': round(sum(b-a for a,b in events)*1000, 3),
                'measured_provider_overlap_ms': round((sum(b-a for a,b in events)-union(events))*1000, 3),
                'measured_injected_delay_union_ms': round(union(delay_events)*1000, 3),
                'measured_injected_delay_overlap_ms': round((sum(b-a for a,b in delay_events)-union(delay_events))*1000, 3),
                'maximum_concurrent_calls': dict(maximum),
                'all_docs_calls_on_owner_thread': all(docs_owner_checks),
                'external_network_calls': 0, 'native_dispatches': 0}
    finally:
        f.doCleanups()


def benchmark(parallel=False, injected_ms=20, desktop=False):
    return {'mode': 'offline_fake_ports_injected_real_sleep',
        'scenarios': [run_scenario(x, parallel=parallel, desktop=desktop, injected_ms=injected_ms)
                      for x in ('prepare_child', 'idle_step', 'queue_event', 'publish_bundle', 'consume_and_ready')],
        'live_startup_observed': False, 'true_ttft_observed': False,
        'sub_180_seconds_verified': False,
        'limits': ['Fixed synthetic controller clock; does not validate elapsed lease deadlines.',
                   'Wall time includes local filesystem and scheduler overhead; injected delays are not provider evidence.',
                   'No meaningful facade content is produced before a committed result.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--parallel', action='store_true')
    parser.add_argument('--desktop', action='store_true')
    parser.add_argument('--injected-ms', type=float, default=20)
    args = parser.parse_args()
    if not 0 <= args.injected_ms <= 1000:
        parser.error('injected latency must be between 0 and 1000 milliseconds')
    print(json.dumps(benchmark(args.parallel, args.injected_ms, args.desktop), indent=2))
