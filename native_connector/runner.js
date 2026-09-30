/* Trusted orchestration only: no credentials, shell, networking, or inference.
 * Copy this released file into native functions.exec, then inject direct tools
 * and offline Python/file callbacks. See README.md for the strict IO contract.
 */
'use strict';

function createNativeConnectorRunner(io, options) {
  options = options || {};
  const concurrency = options.concurrency === undefined ? 3 : options.concurrency;
  const maxAge = options.maxDispatchAgeMs === undefined ? 10000 : options.maxDispatchAgeMs;
  const now = options.now || (typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? () => performance.now() : undefined);
  const required = ['startBatch', 'upload', 'capture', 'recordUpload', 'getMetadata',
    'fetchRaw', 'materializeRaw', 'verifyUpload', 'finalize', 'readControl',
    'planCas', 'reserveWrite', 'casWrite', 'acceptCas'];
  if (!io || required.some(name => typeof io[name] !== 'function') ||
      !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 3 ||
      !Number.isFinite(maxAge) || maxAge <= 0 || maxAge > 10000 ||
      typeof now !== 'function' || (options.onTiming !== undefined && typeof options.onTiming !== 'function')) {
    throw new Error('invalid_runner_contract');
  }
  let lastTime = -Infinity;
  let clockQueue = Promise.resolve();
  const operations = new Set();
  const verifiedBatches = new WeakMap();

  function fault(stage, code) { return Object.freeze({runnerFault: true, stage, code}); }
  function check(condition, stage, code) { if (!condition) throw fault(stage, code); }
  function safeError(error, stage) {
    // Never return callback/provider errors, strings, stacks, or request data.
    return error && error.runnerFault === true && KNOWN_STAGES.has(error.stage) && KNOWN_CODES.has(error.code)
      ? Object.freeze({stage: error.stage, code: error.code})
      : Object.freeze({stage, code: 'callback_failed'});
  }
  function clock(stage) {
    // Native functions.exec may lack performance. A trusted async monotonic
    // bridge is supported; serial sampling prevents completion-order races.
    const sample = clockQueue.then(async () => {
      let value;
      try { value = await now(); } catch (_) { throw fault(stage, 'invalid_monotonic_clock'); }
      check(Number.isFinite(value) && value >= lastTime, stage, 'invalid_monotonic_clock');
      lastTime = value;
      return value;
    });
    clockQueue = sample.catch(() => undefined);
    return sample;
  }
  function clone(value, stage) {
    // Tool arguments and Python descriptors are JSON data, never executable text.
    try {
      const copy = JSON.parse(JSON.stringify(value));
      function freeze(v) {
        if (v && typeof v === 'object') { Object.values(v).forEach(freeze); Object.freeze(v); }
        return v;
      }
      return freeze(copy);
    } catch (_) { throw fault(stage, 'invalid_packet'); }
  }
  function str(v) { return typeof v === 'string' && v.length > 0; }
  function hash(v) { return typeof v === 'string' && /^[0-9a-f]{64}$/.test(v); }
  function body(v) { return v && Object.prototype.hasOwnProperty.call(v, 'structuredContent') ? v.structuredContent : v; }
  function metric(timings, stage, start, end, outcome) {
    const item = Object.freeze({stage, outcome, elapsed_ms: end - start});
    timings.push(item);
    if (options.onTiming) { try { options.onTiming(item); } catch (_) { /* optional telemetry is non-authoritative */ } }
  }
  async function local(stage, fn) {
    try { return await fn(); } catch (error) { throw fault(stage, safeError(error, stage).code); }
  }
  async function rpc(stage, context, callback, timings, freshnessSnapshot) {
    const start = await clock(stage);
    if (freshnessSnapshot) check(start - freshnessSnapshot.dispatchedAt <= maxAge,
      stage, 'dispatch_age_exceeded');
    let response, end;
    try { response = await callback(); }
    catch (_) {
      try { end = await clock(stage); metric(timings, stage, start, end, 'threw'); } catch (_) { /* preserve unknown outcome */ }
      throw fault(stage, 'rpc_outcome_unknown');
    }
    // Persist before even sampling an async clock, which may use a local tool.
    const captured = await local('capture', () => io.capture({stage, context, response}));
    check(str(captured), 'capture', 'invalid_capture');
    end = await clock(stage);
    metric(timings, stage, start, end, 'returned');
    check(!(response && response.isError === true), stage, 'tool_returned_error');
    return {response: clone(response, stage), capture: captured, dispatchedAt: start};
  }
  function validateObject(object) {
    check(object && hash(object.object_id) && str(object.path) && str(object.name) &&
      object.mime_type === 'application/json' && str(object.folder_id), 'start_batch', 'invalid_packet');
  }
  async function uploadVerifiedBatch(request) {
    const timings = [];
    let batch;
    try {
      check(request && Number.isInteger(request.seq) && request.seq > 0, 'start_batch', 'invalid_packet');
      batch = clone(await local('start_batch', () => io.startBatch({seq: request.seq})), 'start_batch');
      check(batch && batch.action === 'upload_batch_once' && str(batch.batch_id) &&
        Array.isArray(batch.objects) && batch.objects.length === 3, 'start_batch', 'invalid_packet');
      batch.objects.forEach(validateObject);
      check(new Set(batch.objects.map(o => o.object_id)).size === 3 &&
        new Set(batch.objects.map(o => o.name)).size === 3 &&
        new Set(batch.objects.map(o => o.folder_id)).size === 1, 'start_batch', 'invalid_packet');
    } catch (error) { return Object.freeze({ok: false, error: safeError(error, 'start_batch'), timings: Object.freeze(timings)}); }

    // All intents are durably consumed by startBatch before any upload dispatch.
    const usedFileIds = new Set();
    let next = 0;
    const tasks = batch.objects.map(() => {
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      return {promise, resolve, reject};
    });
    // Attach handlers before starting workers, so immediate rejection is handled.
    const settlement = Promise.allSettled(tasks.map(task => task.promise));
    async function chain(object) {
      const context = Object.freeze({batch_id: batch.batch_id, object_id: object.object_id});
      const upload = await rpc('upload', context, () => io.upload({object}), timings);
      const recorded = clone(await local('record_upload', () => io.recordUpload({
        batch_id: batch.batch_id, object_id: object.object_id, response: upload.capture
      })), 'record_upload');
      const reference = recorded && recorded.reference;
      check(reference && reference.object_id === object.object_id && reference.locator &&
        reference.locator.backend === 'drive' && reference.locator.folder_id === object.folder_id &&
        str(reference.locator.file_id), 'record_upload', 'invalid_reference');
      const fileId = reference.locator.file_id;
      check(!usedFileIds.has(fileId), 'record_upload', 'duplicate_file_id');
      usedFileIds.add(fileId);
      const metadata = await rpc('metadata', context, () => io.getMetadata({reference}), timings);
      const meta = body(metadata.response);
      check(meta && meta.id === fileId && meta.title === object.name &&
        meta.mime_type === object.mime_type && Array.isArray(meta.parent_ids) &&
        meta.parent_ids.every(str) && meta.parent_ids.includes(object.folder_id), 'metadata', 'metadata_mismatch');
      const raw = await rpc('raw_fetch', context, () => io.fetchRaw({reference,
        metadata: metadata.response, metadata_capture: metadata.capture}), timings);
      const rawFile = await local('materialize', () => io.materializeRaw({reference, response: raw.capture}));
      check(str(rawFile), 'materialize', 'invalid_capture');
      const verified = await local('verify_upload', () => io.verifyUpload({
        batch_id: batch.batch_id, object_id: object.object_id, metadata: metadata.capture, raw_file: rawFile
      }));
      check(verified && verified.verified === true, 'verify_upload', 'verification_failed');
      return Object.freeze({object_id: object.object_id, reference, metadata: metadata.capture, raw_file: rawFile});
    }
    async function worker() {
      while (next < batch.objects.length) {
        const index = next++;
        try { tasks[index].resolve(await chain(batch.objects[index])); }
        catch (error) { tasks[index].reject(safeError(error, 'verify_upload')); }
      }
    }
    await Promise.allSettled(Array.from({length: concurrency}, () => worker()));
    const settled = await settlement;
    const outcomes = Object.freeze(settled.map((item, index) => Object.freeze({
      object_id: batch.objects[index].object_id, ...item
    })));
    if (settled.some(item => item.status !== 'fulfilled')) {
      return Object.freeze({ok: false, batch_id: batch.batch_id, outcomes, timings: Object.freeze(timings)});
    }
    try {
      const items = Object.freeze(settled.map(item => item.value));
      const barrier = await local('finalize', () => io.finalize({batch_id: batch.batch_id, items}));
      check(barrier && barrier.verified === true && barrier.manifest !== undefined,
        'finalize', 'verification_failed');
      const manifest = clone(barrier.manifest, 'finalize');
      const result = Object.freeze({ok: true, batch_id: batch.batch_id, outcomes, manifest, timings: Object.freeze(timings)});
      verifiedBatches.set(result, manifest);
      return result;
    } catch (error) {
      return Object.freeze({ok: false, batch_id: batch.batch_id, outcomes,
        error: safeError(error, 'finalize'), timings: Object.freeze(timings)});
    }
  }

  async function fresh(snapshot, stage) {
    check(await clock(stage) - snapshot.dispatchedAt <= maxAge, stage, 'dispatch_age_exceeded');
  }
  async function casCycle(expectedKind, evidence) {
    const timings = [];
    try {
      const snapshot = await rpc('control_read', Object.freeze({purpose: 'plan'}),
        () => io.readControl({purpose: 'plan'}), timings);
      await fresh(snapshot, 'plan_cas');
      const plan = clone(await local('plan_cas', () => io.planCas({snapshot: snapshot.capture, evidence})), 'plan_cas');
      check(plan && plan.action === 'cas_write_once' && plan.kind === expectedKind &&
        hash(plan.operation_id) && plan.tool_arguments && typeof plan.tool_arguments === 'object', 'plan_cas', 'invalid_packet');
      const args = plan.tool_arguments;
      const snapshotBody = body(snapshot.response);
      check(args.write_control && str(args.write_control.requiredRevisionId) &&
        !Object.prototype.hasOwnProperty.call(args.write_control, 'targetRevisionId') &&
        str(args.document_id) && snapshotBody && snapshotBody.documentId === args.document_id &&
        snapshotBody.revisionId === args.write_control.requiredRevisionId,
        'plan_cas', 'revision_mismatch');
      check(!operations.has(plan.operation_id), 'reserve_write', 'operation_already_attempted');
      await fresh(snapshot, 'reserve_write');
      operations.add(plan.operation_id); // local fail-closed fence, including an unknown reserve result
      const reserved = await local('reserve_write', () => io.reserveWrite({operation_id: plan.operation_id}));
      check(reserved && reserved.reserved === true, 'reserve_write', 'reservation_failed');
      // The RPC start sample checks age after the durable claim and immediately
      // before external dispatch; no extra clock/tool call may intervene.
      const response = await rpc('cas_write', Object.freeze({operation_id: plan.operation_id}),
        () => io.casWrite({tool_arguments: args}), timings, snapshot);
      // This is another actual tool call, never the plan snapshot or cached JSON.
      const readback = await rpc('control_read', Object.freeze({purpose: 'readback'}),
        () => io.readControl({purpose: 'readback'}), timings);
      const accepted = clone(await local('accept_cas', () => io.acceptCas({plan,
        response: response.capture, readback: readback.capture})), 'accept_cas');
      check(accepted && accepted.action === (expectedKind === 'begin' ? 'fetch_request_once' : 'read_control'),
        'accept_cas', 'verification_failed');
      return Object.freeze({ok: true, kind: expectedKind, accepted, timings: Object.freeze(timings)});
    } catch (error) {
      return Object.freeze({ok: false, error: safeError(error, 'plan_cas'), timings: Object.freeze(timings)});
    }
  }
  async function freshCasCycle(request) {
    if (!request || !['claim', 'begin'].includes(request.expectedKind)) {
      return Object.freeze({ok: false, error: Object.freeze({stage: 'plan_cas', code: 'invalid_packet'}), timings: Object.freeze([])});
    }
    return casCycle(request.expectedKind, request.evidence);
  }
  async function claimAndBegin(request) {
    const claim = await freshCasCycle({expectedKind: 'claim', evidence: request && request.evidence});
    if (!claim.ok) return Object.freeze({ok: false, claim});
    const begin = await freshCasCycle({expectedKind: 'begin', evidence: request && request.evidence});
    return Object.freeze({ok: begin.ok, claim, begin});
  }
  async function commitVerifiedBatch(batchResult) {
    if (!batchResult || !verifiedBatches.has(batchResult)) {
      return Object.freeze({ok: false, error: Object.freeze({stage: 'finalize', code: 'verified_barrier_required'}), timings: Object.freeze([])});
    }
    const evidence = verifiedBatches.get(batchResult);
    verifiedBatches.delete(batchResult); // even a failed/unknown commit cannot be automatically replayed
    return casCycle('result', evidence);
  }
  async function uploadAndCommit(request) {
    const batch = await uploadVerifiedBatch(request);
    if (!batch.ok) return Object.freeze({ok: false, batch});
    const commit = await commitVerifiedBatch(batch);
    return Object.freeze({ok: commit.ok, batch, commit});
  }
  return Object.freeze({uploadVerifiedBatch, commitVerifiedBatch, uploadAndCommit, freshCasCycle, claimAndBegin});
}

// Constants contain only public, fixed codes. No error text can become telemetry.
const KNOWN_STAGES = new Set(['start_batch', 'upload', 'capture', 'record_upload', 'metadata',
  'raw_fetch', 'materialize', 'verify_upload', 'finalize', 'control_read', 'plan_cas',
  'reserve_write', 'cas_write', 'accept_cas']);
const KNOWN_CODES = new Set(['callback_failed', 'invalid_monotonic_clock', 'invalid_packet',
  'invalid_capture', 'rpc_outcome_unknown', 'tool_returned_error', 'invalid_reference',
  'duplicate_file_id', 'metadata_mismatch', 'verification_failed', 'dispatch_age_exceeded',
  'revision_mismatch', 'operation_already_attempted', 'reservation_failed', 'verified_barrier_required']);
if (typeof module !== 'undefined' && module.exports) module.exports = {createNativeConnectorRunner};
