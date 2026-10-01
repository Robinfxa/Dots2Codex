/* One invocation by an already active, authorized native controller. No timer,
 * daemon, native spawn, retry loop, credential handling, or automatic wake.
 * Paste this reviewed source in functions.exec alongside tool_adapter.js.
 */
function createGlobalControllerCell(io) {
  'use strict';
  for (const name of ['read','plan','check','write','verify'])
    if(typeof io[name]!=='function')throw Error('global_cell_ports_required');
  let busy=false;
  async function execute(kind, extra={}) {
    const source=await io.read();
    const plan=await io.plan(kind,source,extra); // durable one-attempt reservation
    await io.check(plan,source); // helper host clock, signed predecessor deadline
    let response=null;
    try { response=await io.write(plan.tool_arguments); }
    catch (_) { /* Unknown outcome: read-only exact event reconciliation once. */ }
    const readback=await io.read();
    return io.verify(plan,response,readback); // local accept precedes any agent yield
  }
  async function exclusive(fn) {
    if(busy)throw Error('global_cell_already_running');
    busy=true;try{return await fn();}finally{busy=false;}
  }
  return {
    heartbeat:()=>exclusive(()=>execute('heartbeat')),
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

function createGlobalControllerToolAdapter(tools, config, captureValue) {
  'use strict';
  if(typeof captureValue!=='function')throw Error('exact_private_capture_required');
  const {cwd,stateDir,nativeTaskId,documentId,tabId,joinCodeFile}=config;
  if(![cwd,stateDir,nativeTaskId,documentId,tabId,joinCodeFile].every(x=>typeof x==='string'&&x.length))
    throw Error('global_cell_config_required');
  const quote=x=>"'"+String(x).replace(/'/g,"'\\''")+"'";
  let serial=0;
  const path=label=>stateDir+'/cell-'+Date.now().toString(36)+'-'+(++serial)+'-'+label+'.json';
  const structured=value=>value&&Object.hasOwn(value,'structuredContent')?value.structuredContent:value;
  async function command(args) {
    const r=await tools.exec_command({cmd:['python3','-B',...args].map(quote).join(' '),
      workdir:cwd,max_output_tokens:12000,yield_time_ms:10000});
    if(r.session_id||r.exit_code!==0)throw Error('global_local_helper_failed');
    return JSON.parse(r.output);
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
      const result=await tools.mcp__codex_apps__google_drive_get_document({document_id:documentId,
        fields:'documentId,revisionId,suggestionsViewMode,tabs'});
      if(result&&result.isError)throw Error('global_document_read_failed');
      const resource=structured(result);
      if(!resource||resource.documentId!==documentId||!resource.revisionId||!resource.tabs)
        throw Error('global_exact_document_resource_required');
      const saved=await captureValue(resource);snapshots.set(saved,resource);return saved;
    },
    async plan(kind,source,extra){
      return helper('plan-'+kind,source,['--save',path('plan'),
        ...(kind==='join'?['--capacity',String(extra.capacity),'--seconds',String(extra.seconds)]:[])]);
    },
    async check(plan,source){
      const started=Date.now();
      const checked=await helper('check-cas',source,['--plan-file',plan.plan_file]);
      const elapsed=(Date.now()-started)/1000;
      if(elapsed<0||!checked.dispatch_allowed||checked.checked_at+elapsed>=checked.execute_before)
        throw Error('global_cas_dispatch_window_expired_no_replay');
    },
    async write(args){
      const value=await tools.mcp__codex_apps__google_drive_batch_update_document(args);
      if(value&&value.isError)throw Error('global_cas_outcome_unknown');
      return structured(value);
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
if(typeof module!=='undefined')module.exports={createGlobalControllerCell,createGlobalControllerToolAdapter};
