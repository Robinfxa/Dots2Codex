/* Bounded startup only in the already-admitted native child. No spawn, timer,
 * inference, or polling. Every mutating helper rechecks the imported handoff.
 * Node execution is solely for offline fakes; actual tools come from functions.exec.
 */
function createRouterPairingRunner(io) {
  'use strict';
  let active=false;
  async function exclusive(fn) {
    if(active)throw Error('router_pairing_cell_busy');
    active=true;
    try{return await fn();}finally{active=false;}
  }
  async function writeOnce(plan, completeReady=false) {
    await io.reserveWrite(plan); // exact packet/revision, current sources/clock, durable one use
    let response=null;
    try{response=await io.casWrite(plan);}
    catch(error){await io.captureFailure('cas_outcome_unknown',error);}
    const readback=await io.readBootstrap();
    if(completeReady&&io.mayHaveBundle(readback)) {
      // The hint grants only an already-authorized fixed-target read. Full
      // exact admission verification occurs before any readiness side effect.
      const next=await io.verifyAndReady(plan,response,readback,await io.readControl());
      if(!next||next.admission_verified!==true)throw Error('router_pairing_cas_unverified');
      if(next.action==='wait_for_bundle')return {verified:true,stage:'WORKER_ADMITTED',snapshot:readback};
      return {verified:true,stage:'BUNDLE_READY',ready_plan:next};
    }
    const verified=await io.verify(plan,response,readback); // exact signed event, including peer advance
    if(!verified||verified.verified!==true)throw Error('router_pairing_cas_unverified');
    return verified;
  }
  async function finish(snapshot, control) {
    const ready=await io.ready(snapshot,control);
    if(ready.action==='wait_for_bundle')return {ok:true,...ready};
    return finishPlan(ready);
  }
  async function finishPlan(ready) {
    const verified=await writeOnce(ready);
    if(!['WORKER_POLLING','CONSUMED'].includes(verified.stage))throw Error('router_pairing_unexpected_ready_stage');
    return {ok:true,action:'paired',stage:verified.stage,worker_runtime:ready.worker_runtime,
      native_execution_required:true,next:'paced_connector_poll'};
  }
  async function guarded(work) {
    try{return await work();}
    catch(error){
      const diagnostic=await io.captureFailure('pairing_stopped_no_replay',error);
      return {ok:false,action:'stop_no_replay',diagnostic};
    }
  }
  return {
    pairOnce:()=>exclusive(()=>guarded(async()=>{
      await io.context(); // no network/probe before the exact parent import is verified
      const source=await io.readBootstrap();
      const prepared=await io.prepare(source); // consumes probe reservation before upload
      const branches=await Promise.allSettled([io.forwardProbe(prepared),io.reverseProbe(prepared)]);
      // Drain both branches even if one failed. Never cancel/reupload an unknown upload.
      if(branches.some(result=>result.status!=='fulfilled'))throw Error('router_pairing_probe_branch_failed');
      // Same bounded invocation: a normal peer cannot advance this waiting
      // bootstrap before admission. Revalidate after probes and retain its exact
      // revision; concurrent edits reject CAS, never authorize refresh/replay.
      const plan=await io.planAdmit(source,branches[0].value,branches[1].value);
      const verified=await writeOnce(plan,true);
      if(verified.stage==='WORKER_ADMITTED')return {ok:true,action:'wait_for_bundle',stage:verified.stage,retry_writes:false};
      // A verified exact admission readback that already contains BUNDLE_READY
      // is a valid fresh CAS snapshot. A concurrent peer change makes CAS fail;
      // it never justifies dropping requiredRevisionId or replaying the plan.
      if(verified.stage!=='BUNDLE_READY')throw Error('router_pairing_unexpected_admission_stage');
      return verified.ready_plan?finishPlan(verified.ready_plan):finish(verified.snapshot,await io.readControl());
    })),
    readyOnce:()=>exclusive(()=>guarded(async()=>{
      await io.context();
      const reads=await Promise.allSettled([io.readBootstrap(),io.readControl()]);
      if(reads.some(result=>result.status!=='fulfilled'))throw Error('router_pairing_ready_read_failed');
      return finish(reads[0].value,reads[1].value);
    }))
  };
}

function createRouterPairingToolAdapter(tools, config) {
  'use strict';
  const {cwd,stateDir,nativeTaskId,documentId,controlDocumentId,handoff,admissionReceipt}=config;
  if(![cwd,stateDir,nativeTaskId,documentId,controlDocumentId,admissionReceipt].every(v=>typeof v==='string'&&v.length)
    ||!handoff||typeof handoff.path!=='string'||typeof handoff.sha256!=='string')throw Error('router_pairing_config_required');
  // Reuse supported capture, upload, exact metadata download and raw-file
  // materialization interfaces. The private imported child directory is a valid
  // capture root before a worker runtime exists; no worker method is invoked.
  const native=createNativeToolAdapter(tools,{cwd,root:stateDir,nativeTaskId});
  const quote=v=>"'"+String(v).replace(/'/g,"'\\''")+"'";
  const structured=v=>v&&Object.hasOwn(v,'structuredContent')?v.structuredContent:v;
  let serial=0;
  const sources=new WeakMap(),pending=[];
  const plans=new WeakMap(),probes=new WeakMap();
  function exactJSON(value) {
    function valid(v) {
      if(v===null||typeof v==='string'||typeof v==='boolean')return;
      if(typeof v==='number'&&Number.isFinite(v))return;
      if(typeof v!=='object'||Object.getOwnPropertySymbols(v).length
        ||!Array.isArray(v)&&Object.getPrototypeOf(v)!==Object.prototype&&Object.getPrototypeOf(v)!==null)
        throw Error('router_pairing_arguments_changed');
      for(const key of Object.keys(v)) {
        const descriptor=Object.getOwnPropertyDescriptor(v,key);
        if(!descriptor||!Object.hasOwn(descriptor,'value')||key==='toJSON')throw Error('router_pairing_arguments_changed');
        valid(descriptor.value);
      }
    }
    valid(value);return JSON.stringify(value);
  }
  function current(record) {
    const now=Date.now(),checked=record.checked,elapsed=(now-record.started)/1000;
    if(!Number.isFinite(now)||!Number.isFinite(record.started)||elapsed<0
      ||record.lastClock!==null&&now<record.lastClock||!checked
      ||!Number.isFinite(checked.checked_at)||!Number.isFinite(checked.execute_before)
      ||checked.execute_before!==config.expires||checked.checked_at>=checked.execute_before
      ||checked.checked_at+elapsed>=checked.execute_before)
      throw Error('router_pairing_dispatch_expired_no_replay');
    record.lastClock=now;
  }
  function remember(plan,started) {
    if(!plan||typeof plan.plan_file!=='string'||!plan.tool_arguments)throw Error('router_pairing_plan_required');
    plans.set(plan,{path:plan.plan_file,args:plan.tool_arguments,json:exactJSON(plan.tool_arguments),
      reserved:false,authorized:false,consumed:false,started,lastClock:null,
      checked:plan.dispatch_check&&{reserved:plan.dispatch_check.reserved,
        checked_at:plan.dispatch_check.checked_at,execute_before:plan.dispatch_check.execute_before}});
    return plan;
  }
  async function command(args,input) {
    let cmd=['python3','-B',...args].map(quote).join(' ');
    if(input!==undefined)cmd="printf '%s' "+quote(input)+' | '+cmd;
    const out=await tools.exec_command({cmd,
      workdir:cwd,max_output_tokens:20000,yield_time_ms:10000});
    if(out.session_id||out.exit_code!==0)throw Error('router_pairing_local_helper_failed');
    try{return JSON.parse(out.output);}catch(_){throw Error('router_pairing_helper_output_invalid');}
  }
  function utf8Bytes(text){let n=0;for(const ch of text){const cp=ch.codePointAt(0);
    n+=cp<128?1:cp<2048?2:cp<65536?3:4;}return n;}
  function held(resource) {
    const handle={},record={json:JSON.stringify(resource),path:null};
    if(typeof record.json!=='string')throw Error('router_pairing_provider_response_required');
    sources.set(handle,record);pending.push(record);return handle;
  }
  async function persist(record) {
    if(record.path===null)record.path=await native.captureValue(JSON.parse(record.json));
    return record.path;
  }
  async function helper(operation,args=[],evidence={}) {
    const envelope={},records={},paths=[];
    for(const [key,value] of Object.entries(evidence)) {
      const record=value&&typeof value==='object'?sources.get(value):null;
      if(record&&record.path===null){envelope[key]=JSON.parse(record.json);records[key]=record;}
      else {
        const path=record?record.path:value;
        if(typeof path!=='string'||!path)throw Error('router_pairing_evidence_required');
        paths.push('--'+key.replace(/_/g,'-'),path);
      }
    }
    let input=Object.keys(envelope).length?JSON.stringify(envelope):undefined;
    if(input!==undefined&&utf8Bytes(quote(input))>60000) {
      for(const [key,record] of Object.entries(records))paths.push('--'+key.replace(/_/g,'-'),await persist(record));
      input=undefined;
    }
    const base=['-m','remote_transport.router_pairing',operation,'--handoff-file',handoff.path,
      '--sha256',handoff.sha256,'--native-task-id',nativeTaskId,'--admission-receipt',admissionReceipt,
      ...args,...paths,...(input===undefined?[]:['--inline-evidence'])];
    function accepted(result) {
      if(input===undefined)return result;
      if(!result||!result.evidence_files||typeof result.evidence_files!=='object')throw Error('router_pairing_evidence_capture_missing');
      for(const [key,record] of Object.entries(records)) {
        if(typeof result.evidence_files[key]!=='string'||!result.evidence_files[key])throw Error('router_pairing_evidence_capture_missing');
        record.path=result.evidence_files[key];
      }
      const {evidence_files,...rest}=result;return rest;
    }
    if(!['plan-admit','ready','verify-ready'].includes(operation))return accepted(await command(base,input));
    const resultFile=stateDir+'/pairing-result-'+Date.now().toString(36)+'-'+(++serial)+'-'+Math.random().toString(36).slice(2)+'.json';
    let part,large=true;
    try{part=await command([...base,'--result-file',resultFile,'--result-large-chunk'],input);}
    catch(error){
      if(error.message!=='router_pairing_helper_output_invalid')throw error;
      // Recover only the saved result after truncation. Never rerun the helper.
      large=false;part=await command(['-m','remote_transport.connector_files','packet-chunk',
        '--path',resultFile,'--offset','0','--max-chars','16384']);
    }
    let offset=0,digest,total,parts=[];
    while(true) {
      if(!part||part.offset_chars!==offset||typeof part.text!=='string'
        ||typeof part.sha256!=='string'||!/^[a-f0-9]{64}$/.test(part.sha256)
        ||!Number.isInteger(part.next_offset_chars)||!Number.isInteger(part.total_chars)
        ||part.next_offset_chars!==offset+Array.from(part.text).length
        ||part.total_chars<part.next_offset_chars||part.total_chars>2*1024*1024
        ||typeof part.eof!=='boolean'||part.eof!==(part.next_offset_chars===part.total_chars)
        ||digest&&digest!==part.sha256||total!==undefined&&total!==part.total_chars
        ||(!part.eof&&part.next_offset_chars<=offset))throw Error('router_pairing_result_chunk_invalid');
      digest=part.sha256;total=part.total_chars;parts.push(part.text);offset=part.next_offset_chars;
      if(part.eof)break;
      try{part=await command(['-m','remote_transport.connector_files','packet-chunk','--path',resultFile,
        '--offset',String(offset),'--max-chars',large?'65536':'16384',...(large?['--large-output']:[]),'--sha256',digest]);}
      catch(error){
        if(!large||error.message!=='router_pairing_helper_output_invalid')throw error;
        large=false;part=await command(['-m','remote_transport.connector_files','packet-chunk','--path',resultFile,
          '--offset',String(offset),'--max-chars','16384','--sha256',digest]);
      }
    }
    try{return accepted(JSON.parse(parts.join('')));}catch(_){throw Error('router_pairing_result_json_invalid');}
  }
  async function readDoc(id) {
    const response=await tools.mcp__codex_apps__google_drive_get_document({document_id:id,
      fields:'documentId,revisionId,suggestionsViewMode,tabs'});
    return held(response);
  }
  async function rawEvidence(fileId) {
    const reference={locator:{file_id:fileId}};
    const metadata=await native.io.getMetadata({reference});
    const metadataHeld=held(metadata);
    const response=await native.io.fetchRaw({reference,metadata});
    const fetchedHeld=held(response);
    const download=await native.downloadRaw({reference,response});
    return {metadata:metadataHeld,fetch:fetchedHeld,download:held(download)};
  }
  async function captureFailure(stage,error) {
    // No connector error message or secret-bearing provider object in summary.
    // Returned provider responses themselves were already durably captured.
    for(const record of pending)await persist(record);
    return native.captureValue({contract:'dots-router-pairing-failure/1',stage,
      category:error instanceof Error?'operation_error':'unknown_operation_error',no_automatic_retry:true});
  }
  const io={
    context:async()=>{
      const current=await helper('context');
      for(const key of ['cwd','stateDir','nativeTaskId','documentId','tabId','controlDocumentId','expires'])
        if(current[key]!==config[key])throw Error('router_pairing_context_changed');
      return current;
    },
    readBootstrap:()=>readDoc(documentId),
    readControl:()=>readDoc(controlDocumentId),
    prepare:async snapshot=>{
      const started=Date.now();
      const prepared=await helper('prepare',[],{snapshot});
      const record={json:exactJSON(prepared),started,lastClock:null,checked:prepared,consumed:false};
      current(record);probes.set(prepared,record);return prepared;
    },
    forwardProbe:prepared=>rawEvidence(prepared.forward_probe.file_id),
    reverseProbe:async prepared=>{
      const record=probes.get(prepared);
      if(!record||record.consumed)throw Error('router_pairing_upload_permit_unavailable');
      record.consumed=true;
      if(record.json!==exactJSON(prepared))throw Error('router_pairing_arguments_changed');
      const probe=JSON.parse(record.json).probe;
      current(record); // no await before the one external upload
      let response;
      try{response=await native.io.upload({object:{path:probe.probe_file,name:probe.file_name,
        mime_type:probe.mime_type,folder_id:probe.folder_id}});}
      catch(error){await captureFailure('probe_upload_outcome_unknown',error);throw error;}
      const responsePath=held(response); // failure path persists even malformed responses
      const value=structured(response);
      if(!value||value.success!==true||typeof value.id!=='string'||!value.id)throw Error('router_pairing_upload_unknown');
      const evidence=await rawEvidence(value.id);
      return {...evidence,upload:responsePath};
    },
    planAdmit:async(snapshot,forward,reverse)=>{
      const started=Date.now();
      return remember(await helper('plan-admit',['--reserve-inline'],
        {snapshot,forward_metadata:forward.metadata,forward_fetch:forward.fetch,forward_download:forward.download,
          reverse_metadata:reverse.metadata,reverse_fetch:reverse.fetch,reverse_download:reverse.download,
          upload_response:reverse.upload}),started);
    },
    reserveWrite:async plan=>{
      const record=plans.get(plan);
      if(!record||record.reserved||record.consumed)throw Error('router_pairing_write_permit_unavailable');
      record.reserved=true; // failed or ambiguous helper responses never permit a retry
      if(record.path!==plan.plan_file||record.args!==plan.tool_arguments||record.json!==exactJSON(plan.tool_arguments))
        throw Error('router_pairing_arguments_changed');
      // The same planning RPC durably reserved the exact packet and checked
      // current source/clock after all phase checks. Include all helper and
      // result-transfer time, never mint a fresh deadline during this check.
      if(!record.checked||record.checked.reserved!==true)throw Error('router_pairing_write_permit_unavailable');
      current(record);record.authorized=true;return record.checked;
    },
    casWrite:async plan=>{
      const record=plans.get(plan);
      if(!record||!record.authorized||record.consumed)throw Error('router_pairing_write_permit_unavailable');
      record.consumed=true; // consume before any validation, throw, or connector call
      if(record.path!==plan.plan_file||record.args!==plan.tool_arguments||record.json!==exactJSON(plan.tool_arguments))
        throw Error('router_pairing_arguments_changed');
      current(record);
      const args=JSON.parse(record.json); // caller-mutated objects never reach a tool
      return held(await native.io.casWrite({tool_arguments:args}));
    },
    mayHaveBundle:source=>{
      // Non-authoritative optimization hint only. Never use a Doc-provided ID,
      // path, code or action. All parsing failures take the ordinary full verify.
      try {
        const record=sources.get(source);if(!record)return false;
        const resource=structured(JSON.parse(record.json)),tab=resource.tabs[0];
        const body=(tab.documentTab||tab).body.content;
        const text=body.flatMap(element=>element.paragraph?element.paragraph.elements:[])
          .map(element=>element.textRun?element.textRun.content:'').join('');
        const prefix='DOTS2CODEX_ROUTER_BOOTSTRAP_BEGIN_V3\n';
        const suffix='\nDOTS2CODEX_ROUTER_BOOTSTRAP_END_V3\n';
        if(!text.startsWith(prefix)||!text.endsWith(suffix))return false;
        return JSON.parse(text.slice(prefix.length,-suffix.length)).stage==='BUNDLE_READY';
      }catch(_){return false;}
    },
    verifyAndReady:async(plan,response,readback,control)=>{
      const started=Date.now();
      const result=await helper('verify-ready',['--plan-file',plan.plan_file,'--reserve-inline'],
        {readback,control_snapshot:control,...(response===null?{}:{response})});
      return result.action==='wait_for_bundle'?result:remember(result,started);
    },
    verify:(plan,response,readback)=>helper('verify',['--plan-file',plan.plan_file],
      {readback,...(response===null?{}:{response})}),
    ready:async(snapshot,control)=>{
      const started=Date.now();
      const result=await helper('ready',['--reserve-inline'],{snapshot,...(control?{control_snapshot:control}:{})});
      return result.action==='wait_for_bundle'?result:remember(result,started);
    },
    captureFailure
  };
  return {io};
}
if(typeof module!=='undefined')module.exports={createRouterPairingRunner,createRouterPairingToolAdapter};
