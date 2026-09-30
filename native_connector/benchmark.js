/* Offline synthetic scheduling benchmark. NOT observed connector performance. */
'use strict';
const assert = require('node:assert/strict');
const {fixture} = require('./test_support');

async function syntheticBenchmark() {
  const latencies = {start_batch: 5, upload: [90, 130, 70], metadata: 35,
    raw_fetch: 75, capture: 2, materialize: 10, verify_upload: 5, finalize: 5,
    control_read: 50, plan_cas: 5, reserve_write: 2, cas_write: 60};
  async function run(concurrency) {
    const f = fixture({latencies, options: {concurrency}, kinds: ['result']});
    const outcome = await f.runner.uploadAndCommit({seq: 1});
    assert.equal(outcome.ok, true);
    return {concurrency, virtual_total_ms: f.clock.now(), rpc_calls: f.timings.length,
      maximum_concurrent_rpc_calls: f.maxActive,
      fresh_control_reads: f.events.filter(event => event.stage === 'control_read').length,
      external_writes: f.events.filter(event => ['upload', 'cas_write'].includes(event.stage)).length};
  }
  const sequential = await run(1);
  const bounded_parallel = await run(3);
  assert.equal(sequential.rpc_calls, bounded_parallel.rpc_calls);
  assert.equal(sequential.fresh_control_reads, 2);
  assert.equal(bounded_parallel.fresh_control_reads, 2);
  assert.equal(bounded_parallel.external_writes, 4);
  assert(bounded_parallel.virtual_total_ms < sequential.virtual_total_ms);
  return {mode: 'offline_deterministic_virtual_latencies_only',
    sequential, bounded_parallel,
    saved_virtual_ms: sequential.virtual_total_ms - bounded_parallel.virtual_total_ms,
    ratio: Number((sequential.virtual_total_ms / bounded_parallel.virtual_total_ms).toFixed(3)),
    claim: 'Scheduling overlap only; unchanged tool/read/write counts. No live latency evidence.'};
}
if (require.main === module) syntheticBenchmark().then(value => process.stdout.write(JSON.stringify(value, null, 2) + '\n'));
module.exports = {syntheticBenchmark};
