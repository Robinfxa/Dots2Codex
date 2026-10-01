'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {createGlobalControllerCell,createGlobalControllerToolAdapter,sanitizeGlobalControllerError}=require('./global_controller_cell');
function fixture({lost=false,failVerify=false}={}){
  const calls=[],diagnostics=[];let epoch=0;
  const io={
    async read(){calls.push('read');return {epoch};},
    async plan(kind,source){calls.push('plan:'+kind);return {tool_arguments:{kind},source,kind};},
    async check(){calls.push('check');},
    async write(args){calls.push('write:'+args.kind);epoch++;if(lost)throw Error('lost');return {epoch};},
    async verify(plan,response,readback){calls.push('verify:'+plan.kind);if(failVerify)throw Error('stale');
      assert.equal(readback.epoch,epoch);return {verified:true,first_heartbeat_required:plan.kind==='join'};},
    async recordFailure(error,context){diagnostics.push({error,context});return 'private-diagnostic-'+diagnostics.length;},
    async nextSecond(){calls.push('nextSecond');}
  };
  return {calls,diagnostics,io,cell:createGlobalControllerCell(io)};
}
test('JOIN is accepted then immediate first heartbeat before returning',async()=>{
  const f=fixture();const result=await f.cell.joinAndFirstHeartbeat({capacity:3,seconds:14400});
  assert.equal(result.verified,true);assert.deepEqual(f.calls,[
    'read','plan:join','check','write:join','read','verify:join','nextSecond',
    'read','plan:heartbeat','check','write:heartbeat','read','verify:heartbeat']);
});
test('Unknown write is only reconciled by readback; no second write',async()=>{
  const f=fixture({lost:true});await f.cell.heartbeat();
  assert.equal(f.calls.filter(x=>x==='write:heartbeat').length,1);
});
test('Failed acceptance stops before another tick or spawn',async()=>{
  const f=fixture({failVerify:true});await assert.rejects(f.cell.joinAndFirstHeartbeat({}),/global_cas_outcome_unknown_no_replay/);
  assert.equal(f.diagnostics[0].error.message,'stale');
  assert.equal(f.calls.filter(x=>x==='write:heartbeat').length,0);
});
test('Concurrent invocation fails closed',async()=>{
  const f=fixture();let release;f.io.read=()=>new Promise(resolve=>release=resolve);
  const pending=f.cell.heartbeat();await assert.rejects(f.cell.heartbeat(),/already_running/);
  // finish the pending call with the original asynchronous read.
  f.io.read=async()=>({epoch:1});release({epoch:0});await pending;
});

test('Closed before JOIN is distinct and never dispatches a write',async()=>{
  const f=fixture();f.io.plan=async()=>{throw Error('global_queue_closed');};
  await assert.rejects(f.cell.joinAndFirstHeartbeat({}),/^Error: global_queue_closed_before_join$/);
  assert.deepEqual(f.calls,['read']);
  assert.equal(f.diagnostics[0].context.outcome,'closed_before_join');
  assert.equal(f.diagnostics[0].context.writeAttempted,false);
});
test('Lost write diagnostic is saved before failed readback, without heartbeat or retry',async()=>{
  const f=fixture({lost:true});let reads=0;
  f.io.read=async()=>{
    f.calls.push('read');if(++reads===2){assert.equal(f.diagnostics.length,1);throw Error('read timeout');}
    return {epoch:0};
  };
  await assert.rejects(f.cell.joinAndFirstHeartbeat({}),/global_cas_outcome_unknown_no_replay/);
  assert.equal(f.calls.filter(x=>x==='write:join').length,1);
  assert.equal(f.calls.filter(x=>x==='write:heartbeat').length,0);
  assert.equal(f.diagnostics.length,2);
  assert.equal(f.diagnostics[0].context.stage,'write');
  assert.equal(f.diagnostics[0].error.message,'lost');
  assert.equal(f.diagnostics[1].context.stage,'readback');
  assert.equal(f.diagnostics[1].context.writeFailure,'private-diagnostic-1');
});
test('Closed after attempted JOIN remains unknown and cannot trigger heartbeat',async()=>{
  const f=fixture({lost:true});f.io.verify=async()=>{throw Error('global_queue_closed');};
  await assert.rejects(f.cell.joinAndFirstHeartbeat({}),/global_cas_outcome_unknown_no_replay/);
  assert.equal(f.diagnostics[1].context.outcome,'cas_outcome_unknown_no_replay');
  assert.equal(f.diagnostics[1].context.writeAttempted,true);
  assert.equal(f.calls.filter(x=>x==='write:heartbeat').length,0);
});
test('Failed private diagnostic capture is neither repeated nor followed by another connector call',async()=>{
  const f=fixture({lost:true});let captures=0;
  f.io.recordFailure=async()=>{captures++;throw Error('global_private_diagnostic_capture_failed_no_replay');};
  await assert.rejects(f.cell.joinAndFirstHeartbeat({}),/global_private_diagnostic_capture_failed_no_replay/);
  assert.equal(captures,1);assert.equal(f.calls.filter(x=>x==='read').length,1);
  assert.equal(f.calls.filter(x=>x==='write:join').length,1);
});

function adapterFixture({writeError=null,writeResult=null,verificationError=null,planError=null,readbackError=null}={}) {
  const captures=[],calls=[];let nextResult,reads=0;
  const tools={
    async mcp__codex_apps__google_drive_get_document(){
      calls.push('read');if(++reads===2&&readbackError)throw readbackError;
      return {structuredContent:{documentId:'synthetic-doc',revisionId:'synthetic-revision',tabs:[]}};
    },
    async mcp__codex_apps__google_drive_batch_update_document(){
      calls.push('write');if(writeError)throw writeError;
      return writeResult||{documentId:'synthetic-doc',replies:[],writeControl:{requiredRevisionId:'revision-next'}};
    },
    async exec_command({cmd}){
      if(cmd.includes("'packet-chunk'")) {
        const text=JSON.stringify(nextResult);return {exit_code:0,output:JSON.stringify({offset_chars:0,text,
          sha256:'synthetic-hash',next_offset_chars:text.length,eof:true})};
      }
      const error=cmd.includes("'verify'")?verificationError:
        cmd.includes("'plan-join'")||cmd.includes("'plan-heartbeat'")?planError:null;
      if(error)return {exit_code:1,output:JSON.stringify(error)};
      if(cmd.includes("'check-cas'"))nextResult={dispatch_allowed:true,checked_at:Date.now()/1000,execute_before:Date.now()/1000+120};
      else if(cmd.includes("'verify'"))nextResult={verified:true,first_heartbeat_required:false};
      else nextResult={plan_file:'/private/synthetic-plan.json',operation_id:'synthetic-operation',
        tool_arguments:{document_id:'synthetic-doc',write_control:{requiredRevisionId:'synthetic-revision'}}};
      return {exit_code:0,output:'null'};
    }
  };
  const adapter=createGlobalControllerToolAdapter(tools,{cwd:'/private/package',stateDir:'/private/state',
    nativeTaskId:'/root/synthetic',documentId:'synthetic-doc',tabId:'synthetic-tab',joinCodeFile:'/private/join.txt'},
    async value=>{captures.push(value);return '/private/capture-'+captures.length;});
  return {adapter,captures,calls,diagnostics:()=>captures.filter(x=>x.contract==='dots-global-controller-diagnostic/1')};
}
test('Tool-shaped write error retains category before closure verification but no provider text',async()=>{
  const f=adapterFixture({writeResult:{isError:true,structuredContent:{error:{code:429,status:'RESOURCE_EXHAUSTED',
    message:'Customer password was Goose 12!?'}}},verificationError:{error:'global_operation_not_observed_no_replay',
    diagnostic:{authenticated:true,queue_closed:true,expected_event_observed:false}}});
  await assert.rejects(f.adapter.cell.joinAndFirstHeartbeat({capacity:2,seconds:60}),/global_cas_outcome_unknown_no_replay/);
  assert.deepEqual(f.calls,['read','write','read']);
  const [write,verify]=f.diagnostics();
  assert.deepEqual(write.error,{category:'rate_limited',http_status:429,provider_code:'RESOURCE_EXHAUSTED'});
  assert.equal(verify.error.local_code,'global_operation_not_observed_no_replay');
  assert.deepEqual(verify.error.readback,{authenticated:true,queue_closed:true,expected_event_observed:false});
  assert.equal(verify.write_attempted,true);assert.equal(verify.retry_allowed,false);assert.equal(verify.native_spawn_allowed,false);
  assert.doesNotMatch(JSON.stringify(f.captures),/Goose|password|RESOURCE_EXHAUSTED.*message/);
});
test('Thrown connector exception and failed readback retain separate safe categories',async()=>{
  const f=adapterFixture({writeError:Object.assign(Error('ETIMEDOUT Authorization Bearer sensitive-token'),{code:'ETIMEDOUT'}),
    readbackError:Object.assign(Error('Failed to write text: Private merger memo for Project Hush'),{status:503})});
  await assert.rejects(f.adapter.cell.joinAndFirstHeartbeat({capacity:2,seconds:60}),/global_cas_outcome_unknown_no_replay/);
  const [write,readback]=f.diagnostics();
  assert.equal(write.error.category,'transport_timeout');assert.equal(write.error.provider_code,'ETIMEDOUT');
  assert.equal(readback.error.category,'provider_unavailable');assert.equal(readback.error.http_status,503);
  assert.doesNotMatch(JSON.stringify(f.captures),/sensitive-token|merger|Hush|Bearer|Authorization/);
  assert.deepEqual(f.calls,['read','write','read']);
});
test('Structured local closed error remains visible before a JOIN write',async()=>{
  const f=adapterFixture({planError:{error:'global_queue_closed'}});
  await assert.rejects(f.adapter.cell.joinAndFirstHeartbeat({capacity:2,seconds:60}),/global_queue_closed_before_join/);
  assert.deepEqual(f.calls,['read']);
  assert.deepEqual(f.diagnostics()[0].error,{category:'local_helper_error',local_code:'global_queue_closed'});
});
test('Arbitrary error fields never become private diagnostics',()=>{
  const secrets=['Customer password was Goose 12!?','Failed to write text: Private merger memo for Project Hush',
    'JOIN=arbitrary-secret','https://private.example/?access_token=secret'];
  for(const secret of secrets) {
    const error={name:secret,message:secret,error:secret,code:secret,status:secret,diagnostic:{authenticated:secret,
      queue_closed:secret,expected_event_observed:secret},stack:secret,request:{secret},content:[{type:'text',text:secret}],
      structuredContent:{error:{message:secret,code:secret,status:secret}}};
    const detail=sanitizeGlobalControllerError(error,'connector_error_response');
    assert.deepEqual(detail,{category:'connector_error_response',readback:{authenticated:false,queue_closed:null,
      expected_event_observed:null}});
    assert.equal(JSON.stringify(detail).includes(secret),false);
  }
});
