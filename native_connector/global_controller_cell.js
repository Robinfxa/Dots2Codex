/* One invocation by an already active, authorized native controller. No timer,
 * daemon, native spawn, retry loop, credential handling, or automatic wake.
 * Paste this reviewed source in functions.exec alongside tool_adapter.js.
 */
function createGlobalControllerCell(io) {
  'use strict';
  for (const name of ['read','plan','check','write','verify','recordFailure'])
    if(typeof io[name]!=='function')throw Error('global_cell_ports_required');
  let busy=false;
  async function execute(kind, extra={}, freshSource=null, verify=io.verify) {
    let stage='read',plan=null,writeAttempted=false,writeFailure=null;
    try {
      const source=freshSource===null?await io.read():freshSource;
      stage='plan';plan=await io.plan(kind,source,extra); // durable one-attempt reservation
      stage='check';await io.check(plan,source); // helper clock, signed predecessor deadline
      let response=null;
      stage='write';writeAttempted=true;
      try { response=await io.write(plan.tool_arguments); }
      catch (error) {
        // Preserve the actual sanitized error BEFORE another tool can fail. An
        // error category is diagnostic evidence, never permission to retry CAS.
        writeFailure=await io.recordFailure(error,{kind,stage,plan,writeAttempted,
          outcome:'cas_outcome_unknown_no_replay',writeFailure:null});
      }
      stage='readback';const readback=await io.read();
      stage='verify';const result=await verify(plan,response,readback);
      // This exact full readback is usable only after acceptance and only by
      // the next operation in this invocation. It is never a cached revision
      // for another cell, and the next plan/check still recheck host time.
      return {result,source:readback};
    } catch (error) {
      // A failed private capture must not itself be blindly repeated.
      if(error.message==='global_private_diagnostic_capture_failed_no_replay')throw error;
      const closedBeforeJoin=!writeAttempted&&['join','join-heartbeat'].includes(kind)
        &&error.message==='global_queue_closed';
      const outcome=writeAttempted?'cas_outcome_unknown_no_replay':
        closedBeforeJoin?'closed_before_join':'not_dispatched';
      await io.recordFailure(error,{kind,stage,plan,writeAttempted,outcome,writeFailure});
      if(writeAttempted)throw Error('global_cas_outcome_unknown_no_replay');
      if(closedBeforeJoin)throw Error('global_queue_closed_before_join');
      throw error;
    }
  }
  async function exclusive(fn) {
    if(busy)throw Error('global_cell_already_running');
    busy=true;try{return await fn();}finally{busy=false;}
  }
  async function acceptedHeartbeat(source=null) {
    const accepted=await execute('heartbeat',{},source);
    if(accepted.result.verified!==true||accepted.result.first_heartbeat_required)
      throw Error('global_heartbeat_unverified');
    return accepted;
  }
  async function claimAndBegin(routeId) {
      if(typeof routeId!=='string'||!routeId||typeof io.heartbeatDue!=='function')
        throw Error('global_route_cell_ports_required');
      const source=await io.read();
      // The host-clock helper selects the ordered claim/begin pair or an
      // immediately due heartbeat/claim/begin triple under one checked exact
      // revision. No component grants authority until the whole group verifies.
      const grouped=await execute('claim-startup',{routeId},source);
      if(grouped.result.verified!==true)throw Error('global_claim_begin_unverified');
      const latest=await io.heartbeatDue(null,grouped.result.heartbeat_due_at)?
        await acceptedHeartbeat(grouped.source):grouped;
      // Native reservation is deliberately later, after every required CAS;
      // no burned spawn is left waiting behind a heartbeat or provider write.
      return {source:latest.source,result:{...latest.result,claim_verified:true,begin_verified:true,native_spawn_prepared:false}};

  }
  async function prepareNative(source,routeId) {
    if(typeof routeId!=='string'||!routeId||typeof io.planNative!=='function')
      throw Error('global_pre_native_cell_ports_required');
    const prepared=await io.planNative(source,{routeId});
    if(!prepared||prepared.tool!=='collaboration.spawn_agent'
        ||typeof prepared.plan_file!=='string'||!prepared.plan_file
        ||!prepared.arguments||typeof prepared.arguments!=='object'||Array.isArray(prepared.arguments)
        ||!Number.isFinite(prepared.execute_before)||!Number.isFinite(prepared.checked_at)
        ||prepared.checked_at>=prepared.execute_before)
      throw Error('global_native_preparation_unverified');
    // The active caller must make the next direct platform call. No tool or
    // awaited work follows exposure; no new deadline is minted on return.
    return {...prepared,route_id:routeId,record_snapshot_file:prepared.record_snapshot_file||source,
      next_direct_platform_tool_required:true,native_spawn_invoked_by_cell:false};
  }
  return {
    heartbeat:()=>exclusive(async()=>(await execute('heartbeat')).result),
    claimAndBegin:({routeId})=>exclusive(async()=>(await claimAndBegin(routeId)).result),
    claimAndPrepareNative:({routeId})=>exclusive(async()=>{
      if(typeof io.planNative!=='function')throw Error('global_pre_native_cell_ports_required');
      const begun=await claimAndBegin(routeId);
      // All due heartbeats and CAS verification finish before native reservation.
      // Only this just-accepted readback is reused, within the same invocation.
      return {...await prepareNative(begun.source,routeId),claim_verified:true,begin_verified:true};
    }),
    joinAndFirstHeartbeat:(limits)=>exclusive(async()=>{
      // The helper samples a real JOIN second and a later first-heartbeat
      // second, then signs their strict ordered pair under one original CAS.
      const joined=await execute('join-heartbeat',limits);
      if(joined.result.verified!==true||joined.result.first_heartbeat_required!==false)
        throw Error('global_first_heartbeat_unverified');
      return joined.result;
    }),
    prepareNative:({routeId})=>exclusive(async()=>{
      if(typeof routeId!=='string'||!routeId||typeof io.planNative!=='function')
        throw Error('global_pre_native_cell_ports_required');
      // Standalone preparation always starts with a fresh full current read.
      return prepareNative(await io.read(),routeId);
    }),
    completeAdmission:({routeId,planFile,actualArgumentsFile,nativeResultFile,recordSnapshotFile})=>exclusive(async()=>{
      if(![routeId,planFile,actualArgumentsFile,nativeResultFile,recordSnapshotFile].every(x=>typeof x==='string'&&x.length)
          ||typeof io.recordNative!=='function'
          ||typeof io.verifyAdmission!=='function'&&typeof io.importAdmission!=='function')
        throw Error('global_post_spawn_cell_ports_required');
      // The caller has already preserved the actual submitted arguments and
      // actual platform result in these private files, even if this cell fails.
      // Never synthesize either from a native plan, or dispatch native tools here.
      // The original exact snapshot identifies the local ledger only. Recording
      // real evidence must survive a late return even if live authority is gone;
      // that snapshot is never reused for a write, native dispatch or import.
      const receipt=await io.recordNative(recordSnapshotFile,{planFile,actualArgumentsFile,nativeResultFile});
      if(!receipt||typeof receipt.admission_receipt_file!=='string'||!receipt.admission_receipt_file
          ||typeof receipt.native_task_id!=='string'||!receipt.native_task_id)
        throw Error('global_native_receipt_required');
      const source=await io.read();
      // The helper samples a later real heartbeat tick when necessary; it never
      // synthesizes a future heartbeat or performs a cadence/polling loop.
      // The two ordered signed events share one exact replacement only after
      // the actual native result is durably recorded. Both must verify together.
      const combinedImport=typeof io.verifyAdmission==='function';
      const admitted=await execute('heartbeat-admitted',{routeId},source,combinedImport?
        (plan,response,readback)=>io.verifyAdmission(plan,response,readback,{routeId,receipt}):io.verify);
      if(admitted.result.verified!==true||admitted.result.first_heartbeat_required!==false)
        throw Error('global_admitted_unverified');
      // Parent import rechecks the exact native evidence, admitted event, CAS,
      // lease and deterministic destination. The adapter performs this bounded
      // continuation in the verification helper, using that same exact readback.
      // Missing/uncertain combined output is never followed by another import.
      const imported=combinedImport?admitted.result.child_import:
        await io.importAdmission(admitted.source,{routeId,receipt});
      if(!imported||imported.imported!==true||imported.route_id!==routeId
          ||imported.native_task_id!==receipt.native_task_id
          ||imported.admission_receipt_file!==receipt.admission_receipt_file
          ||typeof imported.child_state_dir!=='string'||!imported.child_state_dir)
        throw Error('global_child_import_unverified');
      return {...imported,admitted_verified:true,heartbeat_due_at:admitted.result.heartbeat_due_at,
        native_spawn_invoked_by_cell:false};
    })
  };
}

/* Diagnostics deliberately contain no remote message, content, request, stack,
 * path, URL or arbitrary string. Error bodies may echo credentials or Docs data,
 * so regex redaction cannot safely preserve their text, even in private files. */
function sanitizeGlobalControllerError(value, category) {
  const root=value&&typeof value==='object'?value:{};
  const structured=root.structuredContent&&typeof root.structuredContent==='object'?root.structuredContent:root;
  const nested=structured.error&&typeof structured.error==='object'?structured.error:structured;
  const providerCodes=new Set(['ABORTED','ALREADY_EXISTS','CANCELLED','DEADLINE_EXCEEDED',
    'FAILED_PRECONDITION','INTERNAL','INVALID_ARGUMENT','NOT_FOUND','OUT_OF_RANGE',
    'PERMISSION_DENIED','RESOURCE_EXHAUSTED','UNAUTHENTICATED','UNAVAILABLE','UNIMPLEMENTED',
    'UNKNOWN','ETIMEDOUT','ECONNRESET','ECONNREFUSED','EAI_AGAIN','rateLimitExceeded',
    'userRateLimitExceeded','quotaExceeded','backendError','forbidden','insufficientPermissions',
    'invalid','notFound','conditionNotMet','authError']);
  const localCodes=new Set(['global_queue_closed','global_queue_closed_before_join',
    'global_operation_not_observed_no_replay','global_cas_acceptance_window_expired_no_replay',
    'global_cas_dispatch_window_expired_no_replay','global_unresolved_cas_readonly_reconciliation_required',
    'global_controller_clock_rollback','global_controller_not_active_restart_required',
    'global_controller_not_active_prepared_event_expired','global_operation_already_issued_no_replay',
    'global_plan_not_reserved','global_response_document_mismatch','global_exact_replace_required',
    'global_response_revision_unverified','global_observation_rollback_or_fork',
    'global_actual_controller_identity_mismatch','global_controller_limits_required',
    'global_controller_join_required','global_controller_cell_source_mismatch',
    'global_document_read_failed','global_exact_document_resource_required',
    'global_result_chunk_invalid','global_cas_outcome_unknown','global_local_helper_failed',
    'global_local_helper_output_invalid','global_private_diagnostic_capture_failed_no_replay',
    'global_cas_dispatch_permit_unavailable','global_cas_dispatch_arguments_mismatch',
    'global_native_dispatch_window_expired_no_replay','global_native_preparation_unverified']);
  const statuses=[nested.status,nested.status_code,nested.statusCode,nested.code,root.status,root.statusCode];
  const status=statuses.find(x=>Number.isInteger(x)&&x>=100&&x<=599);
  const rpc=Number.isInteger(nested.code)&&nested.code>=0&&nested.code<=16?nested.code:undefined;
  const code=[nested.code,nested.status,nested.reason,root.code].find(x=>providerCodes.has(x));
  const local=[root.error,root.message].find(x=>localCodes.has(x));
  // Message text can classify an error, but never leaves this function. Labels
  // are hints only: no error response releases a burned CAS reservation.
  const texts=[root.message,nested.message];
  if(Array.isArray(root.content))for(const c of root.content.slice(0,8))
    if(c&&c.type==='text'&&typeof c.text==='string')texts.push(c.text.slice(0,8192));
  const message=texts.filter(x=>typeof x==='string').map(x=>x.slice(0,8192)).join(' ');
  if(category==='connector_error_response'||category==='connector_exception') {
    if(status===401||status===403||['PERMISSION_DENIED','UNAUTHENTICATED'].includes(code))category='authorization_error';
    else if(status===429||['RESOURCE_EXHAUSTED','rateLimitExceeded','userRateLimitExceeded','quotaExceeded'].includes(code))category='rate_limited';
    else if(status>=500||['UNAVAILABLE','INTERNAL','backendError'].includes(code))category='provider_unavailable';
    else if(status===409||/requiredRevisionId|revision.*(?:mismatch|does not match)/i.test(message))
      category='revision_conflict_reported';
    else if(['DEADLINE_EXCEEDED','ETIMEDOUT','ECONNRESET'].includes(code)||/timeout|timed out|ECONNRESET|ETIMEDOUT/i.test(message))
      category='transport_timeout';
  }
  const result={category};
  if(status!==undefined)result.http_status=status;
  if(rpc!==undefined)result.rpc_status=rpc;
  if(code!==undefined)result.provider_code=code;
  if(local!==undefined)result.local_code=local;
  const d=root.diagnostic;
  if(d&&typeof d==='object')result.readback={
    authenticated:d.authenticated===true,
    queue_closed:typeof d.queue_closed==='boolean'?d.queue_closed:null,
    expected_event_observed:typeof d.expected_event_observed==='boolean'?d.expected_event_observed:null
  };
  return result;
}

function createGlobalControllerToolAdapter(tools, config, captureValue) {
  'use strict';
  if(typeof captureValue!=='function')throw Error('exact_private_capture_required');
  const {cwd,stateDir,nativeTaskId,documentId,tabId,joinCodeFile,nativePlanFile}=config;
  if(![cwd,stateDir,nativeTaskId,documentId,tabId,joinCodeFile].every(x=>typeof x==='string'&&x.length))
    throw Error('global_cell_config_required');
  if(nativePlanFile!==undefined&&(typeof nativePlanFile!=='string'||!nativePlanFile))
    throw Error('global_cell_config_required');
  const quote=x=>"'"+String(x).replace(/'/g,"'\\''")+"'";
  let serial=0;
  const failures=new WeakMap();
  const helperCodes=new Set(['global_queue_closed','global_operation_not_observed_no_replay',
    'global_cas_acceptance_window_expired_no_replay','global_cas_dispatch_window_expired_no_replay',
    'global_unresolved_cas_readonly_reconciliation_required','global_controller_clock_rollback',
    'global_controller_not_active_restart_required','global_native_dispatch_window_expired_no_replay']);
  function failure(code,value,category) {
    const error=Error(code);failures.set(error,sanitizeGlobalControllerError(value,category));return error;
  }
  const path=label=>stateDir+'/cell-'+Date.now().toString(36)+'-'+(++serial)+'-'+label+'.json';
  const structured=value=>value&&Object.hasOwn(value,'structuredContent')?value.structuredContent:value;
  async function command(args,input) {
    let cmd=['python3','-B',...args].map(quote).join(' ');
    if(input!==undefined)cmd="printf '%s' "+quote(input)+' | '+cmd;
    let r;
    try{r=await tools.exec_command({cmd,
      workdir:cwd,max_output_tokens:100000,yield_time_ms:10000});}
    catch(error){throw failure('global_local_helper_failed',error,'local_helper_error');}
    if(r.session_id||r.exit_code!==0) {
      let detail;try{detail=JSON.parse(r.output);}catch(_){detail={message:r.output};}
      throw failure(helperCodes.has(detail&&detail.error)?detail.error:'global_local_helper_failed',detail,'local_helper_error');
    }
    try{return JSON.parse(r.output);}
    catch(error){throw failure('global_local_helper_output_invalid',error,'local_helper_error');}
  }
  const sources=new WeakMap();
  function utf8Bytes(text){let count=0;for(const ch of text){const cp=ch.codePointAt(0);
    count+=cp<128?1:cp<2048?2:cp<65536?3:4;}return count;}
  async function savedSource(source){
    const record=source&&typeof source==='object'?sources.get(source):null;
    if(!record)return source;
    if(record.path===null)record.path=await captureValue(JSON.parse(record.json));
    return record.path;
  }
  async function helper(operation,source,args=[],response=undefined) {
    const record=source&&typeof source==='object'?sources.get(source):null;
    let input;
    if(record&&record.path===null){
      const envelope=JSON.stringify({snapshot:JSON.parse(record.json),response:response===undefined?null:response});
      // Quote-aware UTF-8 bound protects shell argv even for adversarial quotes
      // and Unicode. Larger exact resources retain the old chunked capture path.
      if(utf8Bytes(quote(envelope))<=60000)input=envelope;
    }
    const snapshot=input===undefined?await savedSource(source):null;
    const base=['-m','remote_transport.global_native',operation,
      ...(input===undefined?['--snapshot',snapshot]:['--inline-evidence']),
      '--document-id',documentId,'--tab-id',tabId,'--join-code-file',joinCodeFile,
      '--state-dir',stateDir,'--native-task-id',nativeTaskId];
    if(operation==='verify'&&input===undefined){
      base.push('--readback',snapshot);
      if(response!==null&&response!==undefined)base.push('--response',await captureValue(response));
    }
    function accepted(value){
      if(input!==undefined){
        if(!value||typeof value.snapshot_file!=='string'||!value.snapshot_file)
          throw Error('global_local_helper_output_invalid');
        record.path=value.snapshot_file;
        const {snapshot_file,response_file,...result}=value;return result;
      }
      return value;
    }
    // Fixed-schema acknowledgements fit bounded output; complete queue/plan
    // payloads retain exact file-backed, digest-bound chunk transfer.
    if(['inspect-heartbeat','check-cas','verify','record-native','import-child-admission'].includes(operation))
      return accepted(await command([...base,...args],input));
    const resultFile=path('result');
    // The helper saves the complete result and emits its first bounded chunk in
    // the same RPC. Continuations remain bound to that exact immutable digest.
    let part,largeChunks=true;
    try{part=await command([...base,'--result-file',resultFile,'--result-first-chunk','--result-large-chunk',...args],input);}
    catch(error){
      if(error.message!=='global_local_helper_output_invalid')throw error;
      // A capped tool output can truncate JSON after the helper already saved
      // the immutable result. Recover only that file at the legacy safe size;
      // never reissue the reservation/capture/helper operation.
      largeChunks=false;part=await command(['-m','remote_transport.connector_files','packet-chunk',
        '--path',resultFile,'--offset','0','--max-chars','16384']);
    }
    let offset=0,digest,total,parts=[];
    while(true){
      if(!part||part.offset_chars!==offset||typeof part.text!=='string'
        ||typeof part.sha256!=='string'||!/^[a-f0-9]{64}$/.test(part.sha256)
        ||!Number.isInteger(part.next_offset_chars)||!Number.isInteger(part.total_chars)
        ||part.next_offset_chars!==offset+Array.from(part.text).length
        ||part.total_chars<part.next_offset_chars||part.total_chars>8*1024*1024
        ||typeof part.eof!=='boolean'||part.eof!==(part.next_offset_chars===part.total_chars)
        ||digest&&part.sha256!==digest||total!==undefined&&part.total_chars!==total
        ||(!part.eof&&part.next_offset_chars<=offset))throw Error('global_result_chunk_invalid');
      digest=part.sha256;total=part.total_chars;parts.push(part.text);offset=part.next_offset_chars;
      if(part.eof)break;
      const chunkArgs=['-m','remote_transport.connector_files','packet-chunk','--path',resultFile,
        '--offset',String(offset),'--sha256',digest];
      try{part=await command([...chunkArgs,'--max-chars',largeChunks?'65536':'16384',
        ...(largeChunks?['--large-output']:[])]);}
      catch(error){
        if(!largeChunks||error.message!=='global_local_helper_output_invalid')throw error;
        largeChunks=false;part=await command([...chunkArgs,'--max-chars','16384']);
      }
    }
    try{return accepted(JSON.parse(parts.join('')));}
    catch(error){throw failure('global_local_helper_output_invalid',error,'local_helper_error');}
  }
  // Only a just-issued plan owns a local permit. This adapter keeps no Docs
  // resource/revision cache; WeakMaps cannot turn old files into authority.
  const planned=new WeakMap(),permits=new WeakMap();
  function currentDispatch(record, expiredCode='global_cas_dispatch_window_expired_no_replay') {
    const now=Date.now(),elapsed=(now-record.started)/1000,checked=record.checked;
    if(!Number.isFinite(now)||!Number.isFinite(record.started)||!Number.isFinite(elapsed)
      ||elapsed<0||record.lastClock!==null&&now<record.lastClock
      ||!checked||checked.dispatch_allowed!==true||!Number.isFinite(checked.checked_at)
      ||!Number.isFinite(checked.execute_before)||!Number.isFinite(record.executeBefore)
      ||checked.execute_before!==record.executeBefore||checked.checked_at>=checked.execute_before
      ||checked.checked_at+elapsed>=checked.execute_before)
      throw Error(expiredCode);
    record.lastClock=now;
  }
  const io={
    async read(){
      let result;
      try{result=await tools.mcp__codex_apps__google_drive_get_document({document_id:documentId,
        fields:'documentId,revisionId,suggestionsViewMode,tabs'});}
      catch(error){throw failure('global_document_read_failed',error,'connector_exception');}
      if(result&&result.isError)throw failure('global_document_read_failed',result,'connector_error_response');
      const resource=structured(result);
      if(!resource||resource.documentId!==documentId||!resource.revisionId||!resource.tabs)
        throw Error('global_exact_document_resource_required');
      // Keep the exact current resource private until the immediately following
      // helper captures it and validates it in one RPC. Nothing here is a cache.
      const source=Object.freeze({});sources.set(source,{json:JSON.stringify(resource),path:null});
      return source;
    },
    async plan(kind,source,extra){
      const started=Date.now();
      if(!Number.isFinite(started))throw Error('global_cas_dispatch_window_expired_no_replay');
      const plan=await helper('plan-'+kind,source,['--save',path('plan'),'--check-cas-now',
        ...(['claim','begin','claim-begin','claim-startup','admitted','heartbeat-admitted'].includes(kind)?['--route-id',extra.routeId]:[]),
        ...(['join','join-heartbeat'].includes(kind)?['--capacity',String(extra.capacity),'--seconds',String(extra.seconds)]:[])]);
      if(!plan||typeof plan!=='object'||typeof plan.plan_file!=='string'||!plan.plan_file
        ||!plan.tool_arguments||typeof plan.tool_arguments!=='object'||Array.isArray(plan.tool_arguments))
        throw Error('global_local_helper_output_invalid');
      // Snapshot the proof and exact generated arguments. The caller cannot
      // mutate a returned object into a different authorization before dispatch.
      planned.set(plan,{source,planFile:plan.plan_file,args:plan.tool_arguments,
        argumentsJson:JSON.stringify(plan.tool_arguments),executeBefore:plan.execute_before,
        checked:plan.dispatch_check&&{dispatch_allowed:plan.dispatch_check.dispatch_allowed,
          checked_at:plan.dispatch_check.checked_at,execute_before:plan.dispatch_check.execute_before},
        started,lastClock:null,consumed:false,authorized:false});
      return plan;
    },
    async heartbeatDue(source,dueAt){
      if(source!==null){
        const status=await helper('inspect-heartbeat',source);
        if(!status.controller||status.first_heartbeat_required)
          throw Error('global_controller_join_required');
        dueAt=status.controller.heartbeat_at+status.controller_timing.heartbeat_interval_seconds;
      }
      if(!Number.isFinite(dueAt))throw Error('global_heartbeat_deadline_required');
      // Scheduling only. The signed host-clock check in each normal CAS helper
      // remains authoritative for freshness, lease and acceptance deadlines.
      return Date.now()/1000>=dueAt;
    },
    async check(plan,source){
      const record=planned.get(plan);
      if(!record||record.consumed||record.authorized||record.source!==source
        ||record.planFile!==plan.plan_file||record.executeBefore!==plan.execute_before
        ||record.args!==plan.tool_arguments||record.argumentsJson!==JSON.stringify(plan.tool_arguments))
        throw Error('global_cas_dispatch_permit_unavailable');
      // Existing ledger.check_plan ran after plan reservation in the same helper
      // RPC. Count every millisecond since before that helper, including all
      // result chunks, against its original signed predecessor deadline.
      try{currentDispatch(record);}
      catch(error){
        // A locally observed expiry/rollback is terminal even before check
        // authorizes dispatch; moving the clock back cannot revive the plan.
        record.consumed=true;throw error;
      }
      record.authorized=true;permits.set(record.args,record);
    },
    async write(args){
      const record=args&&typeof args==='object'?permits.get(args):null;
      if(!record||record.consumed)throw Error('global_cas_dispatch_permit_unavailable');
      // Consume before any validation or external call: expiry, mutation, thrown
      // errors and lost responses cannot make this attempt reusable.
      permits.delete(args);record.consumed=true;
      if(record.argumentsJson!==JSON.stringify(args))throw Error('global_cas_dispatch_arguments_mismatch');
      // Dispatch a private parsed copy, never the caller-reachable object. A
      // changed toJSON/getter cannot spoof the comparison and alter real args.
      const submittedArgs=JSON.parse(record.argumentsJson);
      currentDispatch(record); // no awaited work between this gate and dispatch
      let value;
      try{value=await tools.mcp__codex_apps__google_drive_batch_update_document(submittedArgs);}
      catch(error){throw failure('global_cas_outcome_unknown',error,'connector_exception');}
      const resource=structured(value);
      if(value&&value.isError||resource&&typeof resource==='object'&&
          (resource.error||resource.errors||resource.success===false))
        throw failure('global_cas_outcome_unknown',value,'connector_error_response');
      return resource;
    },
    async recordFailure(error,context){
      const detail=failures.get(error)||sanitizeGlobalControllerError(error,'controller_error');
      try{return await captureValue({contract:'dots-global-controller-diagnostic/1',
        operation:context.kind,stage:context.stage,outcome:context.outcome,
        write_attempted:context.writeAttempted,write_failure_capture:context.writeFailure,
        plan_file:context.plan&&context.plan.plan_file||null,
        operation_id:context.plan&&context.plan.operation_id||null,
        group_id:context.plan&&context.plan.group_id||null,
        operation_ids:context.plan&&context.plan.operation_ids||null,
        retry_allowed:false,native_spawn_allowed:false,error:detail});}
      catch(_){throw Error('global_private_diagnostic_capture_failed_no_replay');}
    },
    async verify(plan,response,readback){
      return helper('verify',readback,['--plan-file',plan.plan_file],response);
    },
    async verifyAdmission(plan,response,readback,{routeId,receipt}){
      return helper('verify',readback,['--plan-file',plan.plan_file,'--import-child-admission',
        '--route-id',routeId,'--admission-receipt',receipt.admission_receipt_file,
        '--child-native-task-id',receipt.native_task_id],response);
    },
    async planNative(source,{routeId}){
      const started=Date.now();
      if(!Number.isFinite(started))throw Error('global_native_dispatch_window_expired_no_replay');
      const plan=await helper('plan-native',source,['--route-id',routeId,'--package-root',cwd,
        '--save',nativePlanFile===undefined?path('native-plan'):nativePlanFile,'--check-native-now']);
      if(!plan||plan.tool!=='collaboration.spawn_agent'
        ||typeof plan.plan_file!=='string'||!plan.plan_file
        ||!plan.arguments||typeof plan.arguments!=='object'||Array.isArray(plan.arguments))
        throw Error('global_native_preparation_unverified');
      const checked=plan.dispatch_check&&{dispatch_allowed:plan.dispatch_check.dispatch_allowed,
        checked_at:plan.dispatch_check.checked_at,execute_before:plan.dispatch_check.execute_before};
      const prepared={tool:plan.tool,arguments:JSON.parse(JSON.stringify(plan.arguments)),
        plan_file:plan.plan_file,record_snapshot_file:sources.get(source)?.path||source,
        execute_before:plan.execute_before,checked_at:checked&&checked.checked_at,
        one_attempt_only:true,retry_allowed:false};
      // Count all helper/chunk latency from before reservation. No awaited work
      // follows this final exposure gate; the caller must dispatch immediately.
      currentDispatch({started,lastClock:null,checked,executeBefore:plan.execute_before},
        'global_native_dispatch_window_expired_no_replay');
      return prepared;
    },
    async recordNative(source,{planFile,actualArgumentsFile,nativeResultFile}){
      return helper('record-native',source,['--plan-file',planFile,
        '--actual-arguments',actualArgumentsFile,'--native-result',nativeResultFile,'--save',path('receipt')]);
    },
    async importAdmission(source,{routeId,receipt}){
      return helper('import-child-admission',source,['--route-id',routeId,
        '--admission-receipt',receipt.admission_receipt_file,'--child-native-task-id',receipt.native_task_id]);
    },
    async nextSecond(){
      // Uses the same host clock as signed event preparation; bounded <1.1s.
      await command(['-c','import time; time.sleep(1.01 - time.time() % 1); print("null")']);
    }
  };
  return {io,cell:createGlobalControllerCell(io)};
}
if(typeof module!=='undefined')module.exports={createGlobalControllerCell,createGlobalControllerToolAdapter,sanitizeGlobalControllerError};
