"""Independent exact-RPC and same-port-serialization startup regressions."""
import unittest

from remote_tests.startup_gateway_benchmark import run_scenario


class StartupLatencyIndependentTests(unittest.TestCase):
    def test_idle_step_observes_one_fresh_snapshot(self):
        result = run_scenario('idle_step', injected_ms=0)
        self.assertEqual(result['counts'], {'docs.get_document': 1})

    def test_queue_event_retains_one_fresh_read_one_write_and_exact_readback(self):
        result = run_scenario('queue_event', injected_ms=0)
        self.assertEqual(result['counts'], {'docs.batch_update_document': 1, 'docs.get_document': 2})

    def test_bundle_publication_keeps_both_writes_and_five_authoritative_gets(self):
        result = run_scenario('publish_bundle', injected_ms=0)
        self.assertEqual(result['counts'], {'docs.batch_update_document': 2, 'docs.get_document': 5,
                                          'drive.get_metadata': 1, 'drive.get_bytes': 1})
        self.assertEqual(result['provider_rpc_calls'], 9)

    def test_consumption_then_ready_keeps_both_writes_and_six_authoritative_gets(self):
        result = run_scenario('consume_and_ready', injected_ms=0)
        self.assertEqual(result['counts'], {'docs.batch_update_document': 2, 'docs.get_document': 6})
        self.assertEqual(result['provider_rpc_calls'], 8)

    def test_parallel_preparation_preserves_rpc_counts_and_never_overlaps_same_port(self):
        serial = run_scenario('prepare_child', parallel=False, injected_ms=10)
        parallel = run_scenario('prepare_child', parallel=True, injected_ms=10)
        self.assertEqual(parallel['counts'], serial['counts'])
        self.assertEqual(serial['maximum_concurrent_calls']['all'], 1)
        self.assertEqual(parallel['maximum_concurrent_calls']['all'], 2)
        self.assertEqual(parallel['maximum_concurrent_calls']['docs'], 1)
        self.assertEqual(parallel['maximum_concurrent_calls']['drive'], 1)
        self.assertEqual(parallel['counts']['drive.create_document_once'], 2)
        self.assertEqual(parallel['counts']['drive.create_bytes'], 1)
        self.assertEqual(parallel['external_network_calls'], 0)

    def test_desktop_overlap_keeps_all_docs_on_recovering_owner_and_no_rpc_savings_claim(self):
        serial = run_scenario('prepare_child', parallel=False, desktop=True, injected_ms=10)
        parallel = run_scenario('prepare_child', parallel=True, desktop=True, injected_ms=10)
        self.assertEqual(serial['provider_rpc_calls'], 14)
        self.assertEqual(parallel['counts'], serial['counts'])
        self.assertEqual(serial['maximum_concurrent_calls']['all'], 1)
        self.assertEqual(parallel['maximum_concurrent_calls']['all'], 2)
        self.assertEqual(parallel['maximum_concurrent_calls']['docs'], 1)
        self.assertEqual(parallel['maximum_concurrent_calls']['drive'], 1)
        self.assertTrue(parallel['all_docs_calls_on_owner_thread'])
        self.assertTrue(parallel['desktop_recovery_wrapper'])
        self.assertEqual(parallel['sum_injected_rpc_delay_ms'], 140)
        self.assertEqual(parallel['native_dispatches'], 0)
        self.assertEqual(parallel['external_network_calls'], 0)


if __name__ == '__main__':
    unittest.main()
