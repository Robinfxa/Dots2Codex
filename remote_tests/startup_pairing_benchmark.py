"""Candidate-only real-helper child pairing counts; no measured legacy baseline."""
import json
from pathlib import Path
import sys
import time


def benchmark():
    sys.path.insert(0, str(Path.cwd()))
    from remote_tests.test_router_pairing import RouterPairingTests
    fixture = RouterPairingTests('test_native_adapter_runs_real_local_helpers_with_synthetic_raw_connectors')
    fixture.setUp()
    try:
        start = time.perf_counter()
        result = fixture.run_native_adapter()
        elapsed = time.perf_counter() - start
        expected = {'docs': 4, 'metadata': 2, 'fetch': 2, 'download': 2, 'upload': 1, 'cas': 2, 'helpers': 5}
        assert result['counts'] == expected
        assert result['operations'] == {'context': 1, 'prepare': 1, 'plan-admit': 1, 'verify-ready': 1, 'verify': 1}
        assert result['outcome']['ok'] and result['outcome']['action'] == 'paired'
        return {'mode': 'offline_real_helpers_synthetic_raw_connector_ports',
            'candidate_only': True, 'paired_and_ledger_verified': True,
            'counts': expected, 'tool_rpc_calls': sum(expected.values()),
            'provider_rpc_calls': sum(expected[key] for key in ('docs', 'metadata', 'fetch', 'upload', 'cas')),
            'raw_materialization_tool_calls': expected['download'],
            'helper_rpc_calls': expected['helpers'], 'helper_operations': result['operations'],
            'local_wall_ms_including_synthetic_peer': round(elapsed * 1000, 3),
            'external_network_calls': 0, 'native_dispatches': 0,
            'baseline_measured': False, 'live_startup_observed': False, 'true_ttft_observed': False,
            'limits': ['No legacy pairing adapter exists for an equivalent measured paired comparison.',
                       'Counts include this child adapter only; the synthetic peer publishes its bootstrap/control response outside these counts.',
                       'Local wall time includes fixture peer work; it is not connector latency, provider throughput, or live startup speed.']}
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    print(json.dumps(benchmark(), indent=2))
