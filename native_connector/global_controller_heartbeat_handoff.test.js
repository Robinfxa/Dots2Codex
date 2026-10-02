'use strict';
// Exact offline helper/connector contracts. No external Google or native calls.
const test=require('node:test');
const assert=require('node:assert/strict');
const {createGlobalControllerToolAdapter}=require('./global_controller_cell');

function fixture(t,{defer=true,changePlan=x=>x,helperMs=0,writeError=null,writeResult=null,verify=true}={}) {
  let clock=100000;
  t.mock.method(Date,'now',()=>clock);
  const calls=[],captures=[],plans=[];
  const deferred={action:'heartbeat_deferred_for_ready',read_only:true,write_attempted:false,
    heartbeat_verified:false,native_spawn_allowed:false,pending_ready_count:2,heartbeat_at:90,
    checked_at:100,observe_before:190,recheck_after_seconds:25};
  const tools={
    async mcp__codex_apps__google_drive_get_document(){
      calls.push('read');return {structuredContent:{documentId:'doc',revisionId:'r1',tabs:[]}};
    },
    async mcp__codex_apps__google_drive_batch_update_document(args){
      calls.push('write');assert.deepEqual(args.write_control,{requiredRevisionId:'r1'});
      if(writeError)throw writeError;
      return writeResult||{documentId:'doc',replies:[{replaceAllText:{occurrencesChanged:1}}]};
    },
    async exec_command({cmd}){
      if(cmd.includes("'verify'")) {
        calls.push('verify');
        return verify?{exit_code:0,output:JSON.stringify({verified:true,first_heartbeat_required:false,
          heartbeat_due_at:125,snapshot_file:'/private/readback'})}:
          {exit_code:1,output:JSON.stringify({error:'global_operation_not_observed_no_replay'})};
      }
      assert.match(cmd,/'plan-heartbeat'/);assert.match(cmd,/'--check-cas-now'/);calls.push('plan');
      let plan=defer?{...deferred}:{plan_file:'/private/plan',operation_id:'heartbeat-one',
        tool_arguments:{document_id:'doc',write_control:{requiredRevisionId:'r1'}},execute_before:190,
        dispatch_check:{dispatch_allowed:true,checked_at:100,execute_before:190}};
      plan=changePlan(plan);plans.push(plan);clock+=helperMs;
      const text=JSON.stringify({...plan,snapshot_file:'/private/snapshot'});
      return {exit_code:0,output:JSON.stringify({offset_chars:0,next_offset_chars:text.length,total_chars:text.length,
        text,sha256:'a'.repeat(64),eof:true})};
    }
  };
  const adapter=createGlobalControllerToolAdapter(tools,{cwd:'/private/package',stateDir:'/private/state',
    nativeTaskId:'/root/controller',documentId:'doc',tabId:'tab',joinCodeFile:'/private/code'},
    async value=>{captures.push(value);return '/private/capture-'+captures.length;});
  return {adapter,calls,captures,plans,deferred,setClock:v=>{clock=v;}};
}

test('Ready handoff returns a paced read-only status without CAS, readback, or a permit',async t=>{
  const f=fixture(t,{helperMs:1500}),result=await f.adapter.cell.heartbeat();
  assert.equal(result.action,'heartbeat_deferred_for_ready');assert.equal(result.pending_ready_count,2);
  assert.equal(result.recheck_after_seconds,23.5);assert.equal(result.observe_before,190);
  assert.equal(result.heartbeat_verified,false);assert.equal(result.native_spawn_allowed,false);
  assert.deepEqual(f.calls,['read','plan']);assert.equal(f.captures.length,0);
  await assert.rejects(f.adapter.io.check(result,{}),/permit_unavailable/);
  await assert.rejects(f.adapter.io.write({}),/permit_unavailable/);
  assert.deepEqual(f.calls,['read','plan']);
});

test('Every deferred invocation rereads current authority and has no reservation to reset',async t=>{
  const f=fixture(t);await f.adapter.cell.heartbeat();await f.adapter.cell.heartbeat();
  assert.deepEqual(f.calls,['read','plan','read','plan']);
  assert.equal(f.plans[0].observe_before,f.plans[1].observe_before);
  assert.equal(f.plans[0].heartbeat_at,f.plans[1].heartbeat_at);
});

test('Transfer reaching the original handoff deadline stops before reporting defer',async t=>{
  const f=fixture(t,{helperMs:90000});
  await assert.rejects(f.adapter.cell.heartbeat(),/readiness_window_expired/);
  assert.deepEqual(f.calls,['read','plan']);assert.equal(f.captures[0].write_attempted,false);
});

test('Clock rollback during deferred helper transfer fails closed',async t=>{
  const f=fixture(t,{helperMs:-1});
  await assert.rejects(f.adapter.cell.heartbeat(),/readiness_window_expired/);
  assert.deepEqual(f.calls,['read','plan']);
});

for(const [label,change] of [
  ['missing current window',p=>{delete p.checked_at;return p;}],
  ['expired window',p=>({...p,observe_before:100})],
  ['no pending routes',p=>({...p,pending_ready_count:0})],
  ['unexpected write arguments',p=>({...p,tool_arguments:{}})],
  ['false read-only claim',p=>({...p,read_only:false})],
  ['manufactured heartbeat acceptance',p=>({...p,heartbeat_verified:true})],
  ['new native authority',p=>({...p,native_spawn_allowed:true})],
  ['unbounded recheck delay',p=>({...p,recheck_after_seconds:26})],
  ['delay beyond authority',p=>({...p,observe_before:110,recheck_after_seconds:11})],
  ['future heartbeat',p=>({...p,heartbeat_at:101})]
])test('Malformed deferred result is rejected: '+label,async t=>{
  const f=fixture(t,{changePlan:change});
  await assert.rejects(f.adapter.cell.heartbeat(),/helper_output_invalid/);
  assert.deepEqual(f.calls,['read','plan']);
});

class FixtureDefiniteCASConflict extends Error {}
for(const [label,options] of [
  ['a genuinely rejected synthetic connector call',{writeError:new FixtureDefiniteCASConflict('requiredRevisionId does not match')}],
  ['a stringly named conflict',{writeError:Object.assign(Error('revision mismatch'),{name:'CASConflict',code:'REVISION_CONFLICT',definitelyNotCommitted:true})}],
  ['untrusted error body claims',{writeResult:{isError:true,structuredContent:{error:{code:409,reason:'revisionConflict',
    definitely_not_committed:true,message:'requiredRevisionId does not match'}}}}],
  ['HTTP 400 after a hidden successful POST retry',{writeResult:{isError:true,structuredContent:{error:{code:400,
    message:'requiredRevisionId does not match'}}}}],
  ['an ambiguous transport timeout',{writeError:Object.assign(Error('ETIMEDOUT'),{code:'ETIMEDOUT'})}]
])test('No authoritative non-commit receipt means no replay: '+label,async t=>{
  const f=fixture(t,{defer:false,verify:false,...options});
  await assert.rejects(f.adapter.cell.heartbeat(),/global_cas_outcome_unknown_no_replay/);
  assert.deepEqual(f.calls,['read','plan','write','read','verify']);assert.equal(f.plans.length,1);
  assert.equal(f.captures.length,2);assert(f.captures.every(x=>x.retry_allowed===false&&x.native_spawn_allowed===false));
  assert.equal(f.captures[0].outcome,'cas_outcome_unknown_no_replay');
});

test('Lost successful heartbeat remains reconcilable by exact verifier with one write',async t=>{
  const f=fixture(t,{defer:false,writeError:Error('lost response'),verify:true});
  assert.equal((await f.adapter.cell.heartbeat()).verified,true);
  assert.deepEqual(f.calls,['read','plan','write','read','verify']);assert.equal(f.plans.length,1);
});
