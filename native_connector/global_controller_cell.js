/* One invocation by an already active, authorized native controller. No timer,
 * daemon, native spawn, retry loop, credential handling, or automatic wake.
 * Paste this reviewed source in functions.exec alongside tool_adapter.js.
 */
function createGlobalControllerCell(io) {
  'use strict';
  for (const name of ['read','plan','check','write','verify','recordFailure'])
    if(typeof io[name]!=='function')throw Error('global_cell_ports_required');
  let busy=false;
  async function execute(kind, extra={}, freshSource=null) {
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
      stage='verify';return await io.verify(plan,response,readback);
    } catch (error) {
      // A failed private capture must not itself be blindly repeated.
      if(error.message==='global_private_diagnostic_capture_failed_no_replay')throw error;
      const closedBeforeJoin=!writeAttempted&&kind==='join'&&error.message==='global_queue_closed';
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
  return {
    heartbeat:()=>exclusive(()=>execute('heartbeat')),
    claimAndBegin:({routeId})=>exclusive(async()=>{
      if(typeof routeId!=='string'||!routeId||typeof io.heartbeatDue!=='function')
        throw Error('global_route_cell_ports_required');
      const source=await io.read();
      const due=await io.heartbeatDue(source);
      async function heartbeat(){
        const result=await execute('heartbeat');
        if(!result.verified||result.first_heartbeat_required)throw Error('global_heartbeat_unverified');
        return result;
      }
      if(due)await heartbeat();
      // Each operation retains its own durable reservation, fresh revision,
      // dispatch check and exact-event readback acceptance. Never pipeline CAS.
      const claim=await execute('claim',{routeId},due?null:source);
      if(!claim.verified)throw Error('global_claim_unverified');
      if(await io.heartbeatDue(null,claim.heartbeat_due_at))await heartbeat();
      const begin=await execute('begin',{routeId});
      if(!begin.verified)throw Error('global_begin_unverified');
      const latest=await io.heartbeatDue(null,begin.heartbeat_due_at)?await heartbeat():begin;
      // Native planning and the real native tool remain outside this bounded
      // cell. In particular, no burned spawn is left waiting behind a CAS.
      return {...latest,claim_verified:true,begin_verified:true,native_spawn_prepared:false};
    }),
    joinAndFirstHeartbeat:(limits)=>exclusive(async()=>{
      const joined=await execute('join',limits);
      if(!joined.verified)throw Error('global_join_unverified');
      // Planning uses integer event seconds. Only a subsecond same-tick delay is
      // permitted here, never a cadence sleep or user/status/model turn gap.
      if(typeof io.nextSecond==='function')await io.nextSecond();
      const heartbeat=await execute('heartbeat');
      if(!heartbeat.verified || heartbeat.first_heartbeat_required)
        throw Error('global_first_heartbeat_unverified');
      return heartbeat;
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
    'global_local_helper_output_invalid','global_private_diagnostic_capture_failed_no_replay']);
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
  const {cwd,stateDir,nativeTaskId,documentId,tabId,joinCodeFile}=config;
  if(![cwd,stateDir,nativeTaskId,documentId,tabId,joinCodeFile].every(x=>typeof x==='string'&&x.length))
    throw Error('global_cell_config_required');
  const quote=x=>"'"+String(x).replace(/'/g,"'\\''")+"'";
  let serial=0;
  const failures=new WeakMap();
  const helperCodes=new Set(['global_queue_closed','global_operation_not_observed_no_replay',
    'global_cas_acceptance_window_expired_no_replay','global_cas_dispatch_window_expired_no_replay',
    'global_unresolved_cas_readonly_reconciliation_required','global_controller_clock_rollback',
    'global_controller_not_active_restart_required']);
  function failure(code,value,category) {
    const error=Error(code);failures.set(error,sanitizeGlobalControllerError(value,category));return error;
  }
  const path=label=>stateDir+'/cell-'+Date.now().toString(36)+'-'+(++serial)+'-'+label+'.json';
  const structured=value=>value&&Object.hasOwn(value,'structuredContent')?value.structuredContent:value;
  async function command(args) {
    let r;
    try{r=await tools.exec_command({cmd:['python3','-B',...args].map(quote).join(' '),
      workdir:cwd,max_output_tokens:12000,yield_time_ms:10000});}
    catch(error){throw failure('global_local_helper_failed',error,'local_helper_error');}
    if(r.session_id||r.exit_code!==0) {
      let detail;try{detail=JSON.parse(r.output);}catch(_){detail={message:r.output};}
      throw failure(helperCodes.has(detail&&detail.error)?detail.error:'global_local_helper_failed',detail,'local_helper_error');
    }
    try{return JSON.parse(r.output);}
    catch(error){throw failure('global_local_helper_output_invalid',error,'local_helper_error');}
  }
  async function helper(operation,source,args=[]) {
    const resultFile=path('result');
    await command(['-m','remote_transport.global_native',operation,'--snapshot',source,
      '--document-id',documentId,'--tab-id',tabId,'--join-code-file',joinCodeFile,
      '--state-dir',stateDir,'--native-task-id',nativeTaskId,'--result-file',resultFile,...args]);
    let offset=0,digest,parts=[];
    while(true){
      const part=await command(['-m','remote_transport.connector_files','packet-chunk','--path',resultFile,
        '--offset',String(offset),'--max-chars','16384',...(digest?['--sha256',digest]:[])]);
      if(part.offset_chars!==offset||typeof part.text!=='string'||digest&&part.sha256!==digest
        ||(!part.eof&&part.next_offset_chars<=offset))throw Error('global_result_chunk_invalid');
      digest=part.sha256;parts.push(part.text);offset=part.next_offset_chars;if(part.eof)break;
    }
    return JSON.parse(parts.join(''));
  }
  const snapshots=new Map();
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
      const saved=await captureValue(resource);snapshots.set(saved,resource);return saved;
    },
    async plan(kind,source,extra){
      return helper('plan-'+kind,source,['--save',path('plan'),
        ...(['claim','begin'].includes(kind)?['--route-id',extra.routeId]:[]),
        ...(kind==='join'?['--capacity',String(extra.capacity),'--seconds',String(extra.seconds)]:[])]);
    },
    async heartbeatDue(source,dueAt){
      if(source!==null){
        const status=await helper('inspect',source);
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
      const started=Date.now();
      const checked=await helper('check-cas',source,['--plan-file',plan.plan_file]);
      const elapsed=(Date.now()-started)/1000;
      if(elapsed<0||!checked.dispatch_allowed||checked.checked_at+elapsed>=checked.execute_before)
        throw Error('global_cas_dispatch_window_expired_no_replay');
    },
    async write(args){
      let value;
      try{value=await tools.mcp__codex_apps__google_drive_batch_update_document(args);}
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
        retry_allowed:false,native_spawn_allowed:false,error:detail});}
      catch(_){throw Error('global_private_diagnostic_capture_failed_no_replay');}
    },
    async verify(plan,response,readback){
      const responsePath=response===null?null:await captureValue(response);
      return helper('verify',readback,['--plan-file',plan.plan_file,'--readback',readback,
        ...(responsePath?['--response',responsePath]:[])]);
    },
    async nextSecond(){
      // Uses the same host clock as signed event preparation; bounded <1.1s.
      await command(['-c','import time; time.sleep(1.01 - time.time() % 1); print("null")']);
    }
  };
  return {io,cell:createGlobalControllerCell(io)};
}
if(typeof module!=='undefined')module.exports={createGlobalControllerCell,createGlobalControllerToolAdapter,sanitizeGlobalControllerError};
