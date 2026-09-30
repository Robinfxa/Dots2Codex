'use strict';
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {createNativeConnectorRunner} = require('./runner');
const digest = value => createHash('sha256').update(value).digest('hex');

// A deterministic event queue: no external calls and no real latency sleeps.
function virtualClock() {
  let time = 0, pumping = false;
  const timers = [];
  function pump() {
    if (pumping || !timers.length) return;
    pumping = true;
    setImmediate(() => {
      timers.sort((a, b) => a.at - b.at);
      time = timers[0].at;
      const ready = timers.filter(timer => timer.at === time);
      timers.splice(0, ready.length);
      pumping = false;
      ready.forEach(timer => timer.resolve());
      // Promise continuations install the next operation before the next tick.
      setImmediate(pump);
    });
  }
  return {now: () => time, advance: ms => { time += ms; }, wait: ms => new Promise(resolve => {
    timers.push({at: time + ms, resolve}); pump();
  })};
}

function fixture(config = {}) {
  const clock = virtualClock();
  const events = [], captures = new Map(), reserved = new Set(), bound = new Set();
  const objects = [0, 1, 2].map(index => ({object_id: digest(`object-${index}`), path: `/private/${index}.json`,
    name: `object-${index}.json`, mime_type: 'application/json', folder_id: 'approved-folder'}));
  let started = false, captureNumber = 0, revision = 1, planNumber = 0;
  let active = 0, maxActive = 0;
  const timings = [];
  function event(stage, details) { events.push({stage, ...details}); }
  async function pause(stage, index = 0) {
    const values = config.latencies && config.latencies[stage];
    if (values !== undefined) await clock.wait(Array.isArray(values) ? values[index] : values);
  }
  async function network(stage, index, fn) {
    active++; maxActive = Math.max(maxActive, active);
    try { await pause(stage, index); return fn(); } finally { active--; }
  }
  function byRef(reference) { return objects.findIndex(object => object.object_id === reference.object_id); }
  const io = {
    async startBatch(request) {
      event('start_batch', {seq: request.seq});
      if (started) throw Error('batch already consumed with PRIVATE /sensitive/path');
      started = true;
      await pause('start_batch');
      event('intents_durable');
      return {action: 'upload_batch_once', batch_id: 'private-batch-id', objects};
    },
    async upload({object}) {
      const index = objects.findIndex(value => value.object_id === object.object_id);
      assert(events.some(e => e.stage === 'intents_durable'));
      event('upload', {index});
      return network('upload', index, () => {
        if (config.uploadThrows === index) throw Error('SECRET provider message /private/path prompt data');
        return {content: [{type: 'text', text: 'entire private tool response'}], structuredContent: {
          success: true, id: config.duplicateIds ? 'same-file' : `file-${index}`, privateExtra: 'must preserve'}};
      });
    },
    async capture({stage, context, response}) {
      event('capture', {rpc: stage, context});
      await pause('capture');
      if (config.captureFails === stage) throw Error('private capture error');
      const handle = `/private/capture-${captureNumber++}`;
      captures.set(handle, structuredClone(response));
      return handle;
    },
    async recordUpload({batch_id, object_id, response}) {
      event('record_upload', {object_id});
      assert.equal(batch_id, 'private-batch-id');
      const saved = captures.get(response);
      assert(saved); assert.equal(saved.structuredContent.success, true);
      const id = saved.structuredContent.id;
      if (!config.allowDuplicateRecord && bound.has(id)) throw Error('duplicate exact ID');
      bound.add(id);
      return {reference: {object_id, locator: {backend: 'drive', folder_id: 'approved-folder', file_id: id}}};
    },
    async getMetadata({reference}) {
      const index = byRef(reference);
      event('metadata', {index, reference});
      return network('metadata', index, () => ({structuredContent: {
        id: config.badMetadata === index ? 'wrong-file' : reference.locator.file_id,
        title: objects[index].name, mime_type: 'application/json', parent_ids: ['approved-folder'],
        url: `https://provider.invalid/returned-only/${index}`, extra: 'full metadata'}}));
    },
    async fetchRaw({reference, metadata, metadata_capture}) {
      const index = byRef(reference);
      event('raw_fetch', {index});
      assert.deepEqual(captures.get(metadata_capture), metadata);
      assert.equal(metadata.structuredContent.url, `https://provider.invalid/returned-only/${index}`);
      return network('raw_fetch', index, () => ({structuredContent: {file_uri: {file_id: `sediment://file_raw${index}`}}, extra: 'full raw tool response'}));
    },
    async materializeRaw({reference, response}) {
      const index = byRef(reference);
      event('materialize', {index});
      assert(captures.get(response)); await pause('materialize', index);
      return `/private/raw-${index}`;
    },
    async verifyUpload({object_id, metadata, raw_file}) {
      const index = objects.findIndex(object => object.object_id === object_id);
      event('verify_upload', {index});
      assert.equal(captures.get(metadata).structuredContent.title, objects[index].name);
      assert.equal(raw_file, `/private/raw-${index}`);
      await pause('verify_upload', index);
      if (config.verifyFails === index) throw Error('payload mismatch SECRET');
      return {verified: true};
    },
    async finalize({items}) {
      event('finalize'); assert.equal(items.length, 3); await pause('finalize');
      return {verified: config.barrierFalse !== true, manifest: '/private/verified-manifest'};
    },
    async readControl({purpose}) {
      event('control_read', {purpose});
      return network('control_read', 0, () => ({structuredContent: {
        documentId: 'private-doc-id', revisionId: `r${revision}`, suggestionsViewMode: 'PREVIEW_WITHOUT_SUGGESTIONS', tabs: [],
        full: 'do not truncate control document'}}));
    },
    async planCas({snapshot, evidence}) {
      event('plan_cas', {evidence}); await pause('plan_cas');
      const saved = captures.get(snapshot).structuredContent;
      const kind = (config.kinds || ['claim', 'begin'])[planNumber++] || 'result';
      const plan = {action: 'cas_write_once', kind, operation_id: digest(config.sameOperation ? 'same' : kind),
        tool_arguments: {document_id: saved.documentId, requests: [{replaceAllText: {replaceText: 'PRIVATE BODY'}}],
          write_control: {requiredRevisionId: saved.revisionId}}};
      if (config.badRevision) plan.tool_arguments.write_control.requiredRevisionId = 'old-revision';
      return plan;
    },
    async reserveWrite({operation_id}) {
      event('reserve_write');
      if (reserved.has(operation_id)) throw Error('replayed private operation');
      reserved.add(operation_id); await pause('reserve_write');
      return {reserved: true};
    },
    async casWrite({tool_arguments}) {
      event('cas_write', {tool_arguments});
      assert(Object.isFrozen(tool_arguments));
      assert.equal(tool_arguments.write_control.requiredRevisionId, `r${revision}`);
      return network('cas_write', 0, () => {
        revision++;
        if (config.casThrows) throw Error('lost after write PRIVATE DETAILS');
        return {structuredContent: {documentId: 'private-doc-id', replies: [{replaceAllText: {occurrencesChanged: 1}}],
          writeControl: {requiredRevisionId: `r${revision}`}}, extra: 'complete CAS response'};
      });
    },
    async acceptCas({plan, response, readback}) {
      event('accept_cas');
      assert.equal(captures.get(response).structuredContent.writeControl.requiredRevisionId,
        captures.get(readback).structuredContent.revisionId);
      return {action: plan.kind === 'begin' ? 'fetch_request_once' : 'read_control'};
    }
  };
  const runner = createNativeConnectorRunner(io, {now: clock.now, onTiming: item => timings.push(item), ...config.options});
  return {runner, io, clock, objects, events, captures, timings, reserved, get maxActive() {return maxActive;}};
}
module.exports = {virtualClock, fixture};
