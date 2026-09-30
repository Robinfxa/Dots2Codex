'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {createNativeConnectorRunner} = require('./runner');
const {fixture} = require('./test_support');
const count = (f, stage) => f.events.filter(event => event.stage === stage).length;

test('three reordered immutable chains preserve exact ID associations and full responses', async () => {
  const f = fixture({latencies: {upload: [30, 10, 20], metadata: [4, 6, 2], raw_fetch: 3}});
  const result = await f.runner.uploadVerifiedBatch({seq: 1});
  assert.equal(result.ok, true); assert.equal(f.maxActive, 3);
  assert.equal(count(f, 'finalize'), 1);
  for (let index = 0; index < 3; index++) {
    assert.equal(result.outcomes[index].value.reference.locator.file_id, `file-${index}`);
    assert.equal(result.outcomes[index].value.object_id, f.objects[index].object_id);
  }
  assert.equal(f.captures.size, 9);
  assert([...f.captures.values()].some(value => value.structuredContent.privateExtra === 'must preserve'));
});

test('allSettled preserves both successes after a lost response and never crosses barrier', async () => {
  const f = fixture({uploadThrows: 1, latencies: {upload: [9, 1, 15]}});
  const result = await f.runner.uploadAndCommit({seq: 1});
  assert.equal(result.ok, false);
  assert.deepEqual(result.batch.outcomes.map(o => o.status), ['fulfilled', 'rejected', 'fulfilled']);
  assert.equal(count(f, 'verify_upload'), 2); assert.equal(count(f, 'finalize'), 0);
  assert.equal(count(f, 'control_read'), 0); assert.equal(count(f, 'upload'), 3);
  assert.equal(result.batch.outcomes[1].reason.code, 'rpc_outcome_unknown');
  assert(!JSON.stringify(result.batch.outcomes[1]).includes('SECRET'));
});

test('verification failure retains successes, prevents fresh result read, and replay does not upload', async () => {
  const f = fixture({verifyFails: 1});
  const first = await f.runner.uploadAndCommit({seq: 1});
  assert.equal(first.ok, false); assert.equal(count(f, 'verify_upload'), 3);
  assert.equal(count(f, 'finalize'), 0); assert.equal(count(f, 'control_read'), 0);
  const second = await f.runner.uploadAndCommit({seq: 1});
  assert.equal(second.ok, false); assert.equal(count(f, 'upload'), 3);
});

test('duplicate returned IDs fail closed even if adapter omits its duplicate check', async () => {
  const f = fixture({duplicateIds: true, allowDuplicateRecord: true});
  const result = await f.runner.uploadVerifiedBatch({seq: 1});
  assert.equal(result.ok, false);
  assert.equal(result.outcomes.filter(o => o.status === 'rejected' && o.reason.code === 'duplicate_file_id').length, 2);
  assert.equal(count(f, 'verify_upload'), 1); assert.equal(count(f, 'finalize'), 0);
});

test('metadata mismatch is captured and never fetched as raw bytes', async () => {
  const f = fixture({badMetadata: 1});
  const result = await f.runner.uploadVerifiedBatch({seq: 1});
  assert.equal(result.ok, false); assert.equal(count(f, 'raw_fetch'), 2);
  assert.equal(result.outcomes[1].reason.code, 'metadata_mismatch');
});

test('capture failures halt dependent stages without retrying external writes', async () => {
  const f = fixture({captureFails: 'upload'});
  const result = await f.runner.uploadVerifiedBatch({seq: 1});
  assert.equal(result.ok, false); assert.equal(count(f, 'upload'), 3);
  assert.equal(count(f, 'record_upload'), 0); assert.equal(count(f, 'finalize'), 0);
});

test('returned error responses are saved in full before stopping', async () => {
  const f = fixture();
  f.io.upload = async () => ({isError: true, content: [{type: 'text', text: 'PRIVATE ERROR'}]});
  const result = await f.runner.uploadVerifiedBatch({seq: 1});
  assert.equal(result.ok, false); assert.equal(f.captures.size, 3);
  assert.equal(count(f, 'record_upload'), 0);
  assert(!JSON.stringify(result).includes('PRIVATE ERROR'));
});

test('claim then begin has four genuinely separate full reads and exact once writes', async () => {
  const f = fixture();
  const result = await f.runner.claimAndBegin({evidence: '/private/manifest'});
  assert.equal(result.ok, true);
  assert.equal(result.begin.accepted.action, 'fetch_request_once');
  assert.deepEqual(f.events.filter(e => e.stage === 'control_read').map(e => e.purpose), ['plan', 'readback', 'plan', 'readback']);
  assert.equal(count(f, 'cas_write'), 2); assert.equal(count(f, 'reserve_write'), 2);
  const significant = f.events.filter(e => ['control_read', 'plan_cas', 'reserve_write', 'cas_write', 'accept_cas'].includes(e.stage));
  assert.deepEqual(significant.map(e => e.stage), ['control_read', 'plan_cas', 'reserve_write', 'cas_write', 'control_read', 'accept_cas',
    'control_read', 'plan_cas', 'reserve_write', 'cas_write', 'control_read', 'accept_cas']);
});

test('result commit requires unforgeable in-memory verified barrier, one attempt only', async () => {
  const f = fixture({kinds: ['result']});
  const fake = await f.runner.commitVerifiedBatch({ok: true, manifest: '/private/fake'});
  assert.equal(fake.error.code, 'verified_barrier_required'); assert.equal(count(f, 'control_read'), 0);
  const batch = await f.runner.uploadVerifiedBatch({seq: 1});
  const copy = await f.runner.commitVerifiedBatch(JSON.parse(JSON.stringify(batch)));
  assert.equal(copy.ok, false); assert.equal(count(f, 'control_read'), 0);
  const committed = await f.runner.commitVerifiedBatch(batch);
  assert.equal(committed.ok, true); assert.equal(count(f, 'cas_write'), 1);
  assert.equal(f.events.find(e => e.stage === 'plan_cas').evidence, batch.manifest);
  assert.equal((await f.runner.commitVerifiedBatch(batch)).ok, false);
  assert.equal(count(f, 'cas_write'), 1);
  assert.equal((await f.runner.freshCasCycle({expectedKind: 'result', evidence: batch.manifest})).ok, false);
});

test('failed Python all-verified barrier prevents result CAS', async () => {
  const f = fixture({barrierFalse: true});
  const result = await f.runner.uploadAndCommit({seq: 1});
  assert.equal(result.ok, false); assert.equal(count(f, 'control_read'), 0);
});

test('CAS lost response has one durable attempt and no accept or automatic replay', async () => {
  const f = fixture({casThrows: true, sameOperation: true});
  const first = await f.runner.freshCasCycle({expectedKind: 'claim'});
  assert.equal(first.ok, false); assert.equal(first.error.code, 'rpc_outcome_unknown');
  const second = await f.runner.freshCasCycle({expectedKind: 'begin'});
  assert.equal(second.error.code, 'operation_already_attempted');
  assert.equal(count(f, 'cas_write'), 1); assert.equal(count(f, 'accept_cas'), 0);
});

test('durable reserve rejects replay across fresh runner instances', async () => {
  const f = fixture({sameOperation: true});
  assert.equal((await f.runner.freshCasCycle({expectedKind: 'claim'})).ok, true);
  const restarted = createNativeConnectorRunner(f.io, {now: f.clock.now});
  assert.equal((await restarted.freshCasCycle({expectedKind: 'begin'})).ok, false);
  assert.equal(count(f, 'cas_write'), 1);
});

test('monotonic age includes read RPC and Python planning, not parsed JSON age', async () => {
  const f = fixture({latencies: {control_read: 4000, plan_cas: 6001}});
  const result = await f.runner.freshCasCycle({expectedKind: 'claim'});
  assert.equal(result.error.code, 'dispatch_age_exceeded'); assert.equal(count(f, 'cas_write'), 0);
});

test('age is rechecked after durable marker; expired claim stays burned without write', async () => {
  const f = fixture({latencies: {reserve_write: 10001}});
  const result = await f.runner.freshCasCycle({expectedKind: 'claim'});
  assert.equal(result.error.code, 'dispatch_age_exceeded'); assert.equal(f.reserved.size, 1);
  assert.equal(count(f, 'cas_write'), 0);
});

test('snapshot revision mismatch prevents reserve and write', async () => {
  const f = fixture({badRevision: true});
  const result = await f.runner.freshCasCycle({expectedKind: 'claim'});
  assert.equal(result.error.code, 'revision_mismatch');
  assert.equal(count(f, 'reserve_write'), 0); assert.equal(count(f, 'cas_write'), 0);
});

test('clock regression fails closed while preserving a returned full response', async () => {
  let calls = 0;
  const f = fixture({options: {now: () => calls++ === 0 ? 100 : 99}});
  const result = await f.runner.freshCasCycle({expectedKind: 'claim'});
  assert.equal(result.error.code, 'invalid_monotonic_clock'); assert.equal(f.captures.size, 1);
  assert.equal(count(f, 'cas_write'), 0);
});

test('timing records have a fixed payload-free schema even with arbitrary provider errors', async () => {
  const f = fixture({uploadThrows: 1, latencies: {upload: 3, metadata: 5, raw_fetch: 7}});
  await f.runner.uploadVerifiedBatch({seq: 1});
  assert(f.timings.length > 0);
  for (const item of f.timings) {
    assert.deepEqual(Object.keys(item).sort(), ['elapsed_ms', 'outcome', 'stage']);
    assert(['returned', 'threw'].includes(item.outcome));
    assert(Number.isFinite(item.elapsed_ms) && item.elapsed_ms >= 0);
    assert(Object.isFrozen(item));
  }
  assert(!/SECRET|private|file-|object-|prompt|path/.test(JSON.stringify(f.timings)));
});

test('bounded concurrency rejects settings above three and accepts sequential baseline', async () => {
  const f = fixture({options: {concurrency: 1}, latencies: {upload: 1}});
  assert.equal((await f.runner.uploadVerifiedBatch({seq: 1})).ok, true); assert.equal(f.maxActive, 1);
  assert.throws(() => createNativeConnectorRunner(f.io, {concurrency: 4}), /invalid_runner_contract/);
  assert.throws(() => createNativeConnectorRunner(f.io, {maxDispatchAgeMs: 10001}), /invalid_runner_contract/);
});

test('async monotonic clocks are serialized across parallel chains', async () => {
  let f, activeSamples = 0, maxSamples = 0;
  f = fixture({latencies: {upload: [3, 1, 2]}, options: {now: async () => {
    activeSamples++; maxSamples = Math.max(maxSamples, activeSamples);
    await Promise.resolve(); activeSamples--; return f.clock.now();
  }}});
  const result = await f.runner.uploadVerifiedBatch({seq: 1});
  assert.equal(result.ok, true); assert.equal(maxSamples, 1);
});

test('native-like realm without performance requires an explicit monotonic clock', async () => {
  const vm = require('node:vm');
  const fs = require('node:fs');
  const sandbox = {module: {exports: {}}};
  vm.runInNewContext(fs.readFileSync(require.resolve('./runner'), 'utf8'), sandbox);
  const create = sandbox.module.exports.createNativeConnectorRunner;
  const f = fixture();
  assert.throws(() => create(f.io), /invalid_runner_contract/);
  const runner = create(f.io, {now: async () => f.clock.now()});
  assert.equal((await runner.claimAndBegin({})).ok, true);
});

test('synthetic benchmark remains deterministic and retains every safety RPC', async () => {
  const {syntheticBenchmark} = require('./benchmark');
  const result = await syntheticBenchmark();
  assert.equal(result.sequential.virtual_total_ms, 866);
  assert.equal(result.bounded_parallel.virtual_total_ms, 444);
  assert.equal(result.sequential.rpc_calls, 12); assert.equal(result.bounded_parallel.rpc_calls, 12);
  assert.equal(result.bounded_parallel.fresh_control_reads, 2);
  assert.equal(result.mode, 'offline_deterministic_virtual_latencies_only');
});
