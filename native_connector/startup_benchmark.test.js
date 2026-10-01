'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {runScenario,compare}=require('./startup_benchmark');
const repo=path.resolve(__dirname,'..');
const cases=[['join',5,2,13000,'join_heartbeat_shared_plan',['join','heartbeat']],
  ['claim_begin',5,2,13000,'claim_begin_shared_plan',['claim','begin']],
  ['claim_begin_due',5,2,13000,'heartbeat_claim_begin_shared_plan',['heartbeat','claim','begin']],
  ['claim_prepare_native',6,3,14000,'shared_plan',['claim','begin']],
  ['admission',6,3,14000,'heartbeat_admitted_shared_plan',['heartbeat','admitted']]];
for(const [scenario,total,helpers,ms,flag,kinds] of cases){
  test('Real helper '+scenario+' preserves exact signed pair in one inline checked CAS',async()=>{
    const r=await runScenario(repo,scenario);
    assert.equal(r.outcome,'verified');assert.equal(r.counts.docs_reads,2);
    assert.equal(r.counts.docs_writes,1);assert.equal(r.counts.check_execs,0);
    assert.equal(r.counts.inline_check_execs,1);assert.equal(r.counts.verify_execs,1);
    assert.equal(r.counts.capture_execs,0);assert.equal(r.counts.inspect_execs,0);
    assert.equal(r.counts.result_chunk_execs,0);assert.equal(r.counts.helper_execs,helpers);
    assert.equal(r.total_rpc_calls,total);assert.equal(r.injected_rpc_critical_path_ms,ms);
    assert.equal(r.injected_rpc_critical_path_ms,r.sum_injected_rpc_delay_ms);
    assert.equal(r.signed_state_evidence[flag],true);
    assert.deepEqual(r.signed_state_evidence.new_event_kinds,kinds);
    assert.equal(r.signed_state_evidence.child_imported,scenario==='admission');
    assert.equal(r.counts.inline_native_check_execs,scenario==='claim_prepare_native'?1:0);
    assert.equal(r.counts.native_plan_execs,scenario==='claim_prepare_native'?1:0);
    assert.equal(r.native_dispatches,0);
  });
  test('Lost '+scenario+' group response reconciles exact prefix without replay',async()=>{
    const r=await runScenario(repo,scenario,{loseWrite:true});
    assert.equal(r.outcome,'verified');assert.equal(r.counts.docs_writes,1);
    assert.equal(r.counts.plan_execs,1);assert.equal(r.counts.verify_execs,1);
    assert.equal(r.signed_state_evidence[flag],true);
    assert.equal(r.signed_state_evidence.unresolved_operation_count,0);
  });
  test('Unreadable '+scenario+' group outcome stays burned without subsequent effect',async()=>{
    const r=await runScenario(repo,scenario,{loseWrite:true,failReadback:true});
    assert.equal(r.outcome,'failed_closed_after_first_write');
    assert.equal(r.counts.docs_writes,1);assert.equal(r.counts.plan_execs,1);
    assert.equal(r.counts.verify_execs,0);
    assert.equal(r.signed_state_evidence.unresolved_operation_count,1);
    assert.deepEqual(r.signed_state_evidence.verified_operation_kinds,[]);
    assert.equal(r.signed_state_evidence.child_imported,false);
  });
  test('Conflicted '+scenario+' group accepts neither event and never retries',async()=>{
    const r=await runScenario(repo,scenario,{conflictOnWrite:1});
    assert.equal(r.outcome,'failed_closed_after_first_write');
    assert.equal(r.counts.docs_writes,1);assert.equal(r.counts.plan_execs,1);
    assert.equal(r.counts.verify_execs,1);
    assert.deepEqual(r.signed_state_evidence.new_event_kinds,[]);
    assert.deepEqual(r.signed_state_evidence.verified_operation_kinds,[]);
    assert.equal(r.signed_state_evidence.unresolved_operation_count,1);
    assert.equal(r.signed_state_evidence.child_imported,false);
  });
}
test('Comparison accepts reduced writes only with complete authenticated group evidence',()=>{
  const proof={authenticated:true,new_event_kinds:['claim','begin'],
    verified_operation_kinds:['begin','claim'],unresolved_operation_count:0,
    reservation_count:2,distinct_reserved_plan_count:2,route_state:'spawn_intent',
    native_spawn_reservations:0,claim_begin_shared_plan:false};
  const before={scenario:'claim_begin',outcome:'verified',counts:{docs_reads:3,docs_writes:2,helper_execs:17},
    signed_state_evidence:proof,total_rpc_calls:22,injected_rpc_critical_path_ms:36000};
  const after={...before,counts:{docs_reads:2,docs_writes:1,helper_execs:2},total_rpc_calls:5,
    injected_rpc_critical_path_ms:13000,signed_state_evidence:{...proof,
      reservation_count:1,distinct_reserved_plan_count:1,claim_begin_shared_plan:true}};
  assert.equal(compare(before,after).saved_rpc_calls,17);
  assert.equal(compare(before,after).grouped_cas_reduction_verified,true);
  for(const change of [{claim_begin_shared_plan:false},{new_event_kinds:['claim']},
      {verified_operation_kinds:['claim']},{unresolved_operation_count:1},
      {route_state:'claimed'},{native_spawn_reservations:1},{authenticated:false}]){
    assert.throws(()=>compare(before,{...after,signed_state_evidence:{...after.signed_state_evidence,...change}}));
  }
});
test('Comparison rejects a silently dropped JOIN write without signed group proof',()=>{
  const r={scenario:'join',outcome:'verified',counts:{docs_writes:2},total_rpc_calls:25,injected_rpc_critical_path_ms:41000};
  assert.throws(()=>compare(r,{...r,counts:{docs_writes:1}}));
});
