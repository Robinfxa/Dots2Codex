'use strict';
// Synthetic tools and clocks only. No provider or native dispatch.
const test=require('node:test');
const assert=require('node:assert/strict');
const {createGlobalControllerToolAdapter}=require('./global_controller_cell');

function fixture(t,{chunkSize=100000,proof={},onCommand=()=>{},mutateChunk=x=>x,truncatePlan=false,truncateContinuation=false}={}) {
  let clock=100000,packet=null,chunkCount=0,truncatedContinuation=false;
  const commands=[],writes=[],captures=[];
  t.mock.method(Date,'now',()=>clock);
  const config={cwd:'/private/package',stateDir:'/private/state',nativeTaskId:'/root/controller',
    documentId:'doc',tabId:'tab',joinCodeFile:'/private/join'};
  function chunk(offset) {
    const chars=Array.from(JSON.stringify(packet)),end=Math.min(offset+chunkSize,chars.length);
    return mutateChunk({offset_chars:offset,next_offset_chars:end,total_chars:chars.length,
      text:chars.slice(offset,end).join(''),sha256:'a'.repeat(64),eof:end===chars.length},chunkCount++);
  }
  const tools={
    async exec_command({cmd,max_output_tokens}){
      assert.equal(max_output_tokens,100000);
      commands.push(cmd);onCommand(cmd,{advance:ms=>{clock+=ms;}});
      let value;
      if(cmd.includes("'packet-chunk'")) {
        if(cmd.includes("'--offset' '0'"))assert.equal(truncatePlan,true);
        else assert.match(cmd,/'--sha256' 'a{64}'/);
        value=chunk(Number(cmd.match(/'--offset' '(\d+)'/)[1]));
      }else if(cmd.includes("'inspect-heartbeat'"))value={controller:{heartbeat_at:100},
        first_heartbeat_required:false,controller_timing:{heartbeat_interval_seconds:25}};
      else if(cmd.includes("'verify'"))value={verified:true,first_heartbeat_required:false,heartbeat_due_at:125};
      else {
        assert.match(cmd,/'--check-cas-now'/);assert.match(cmd,/'--result-first-chunk'/);
        packet={plan_file:'/private/plan',group_id:'group-one',operation_ids:['claim-one','begin-one'],
          execute_before:110,dispatch_check:{dispatch_allowed:true,checked_at:100,execute_before:110,...proof},
          tool_arguments:{document_id:'doc',write_control:{requiredRevisionId:'exact-revision'},
            requests:[{replaceAllText:{replaceText:'exact 😀 文本',containsText:{text:'old'}}}]}};
        value=chunk(0);
      }
      if(truncatePlan&&cmd.includes("'plan-claim-begin'"))return {exit_code:0,output:'{\"truncated\":'};
      if(truncateContinuation&&cmd.includes("'packet-chunk'")&&!truncatedContinuation){
        truncatedContinuation=true;return {exit_code:0,output:'{\"truncated\":'};}
      return {exit_code:0,output:JSON.stringify(value)};
    },
    async mcp__codex_apps__google_drive_batch_update_document(args){writes.push(structuredClone(args));return {documentId:'doc'};},
    async mcp__codex_apps__google_drive_get_document(){return {documentId:'doc',revisionId:'exact-revision',tabs:[]};}
  };
  const adapter=createGlobalControllerToolAdapter(tools,config,async value=>{
    captures.push(value);return '/private/capture-'+captures.length;});
  return {adapter,commands,writes,captures,advance:ms=>{clock+=ms;},setClock:value=>{clock=value;},
    plan:()=>adapter.io.plan('claim-begin','/private/exact-source',{routeId:'route-one'})};
}

test('Plan, ledger check and first result chunk share one RPC; actual exact write consumes its permit',async t=>{
  const f=fixture(t),plan=await f.plan();
  assert.equal(f.commands.length,1);
  await f.adapter.io.check(plan,'/private/exact-source');
  assert.equal(f.commands.length,1);
  await f.adapter.io.write(plan.tool_arguments);
  assert.deepEqual(f.writes,[plan.tool_arguments]);
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/permit_unavailable/);
  await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/permit_unavailable/);
  assert.equal(f.writes.length,1);
});
test('Multi-chunk Unicode result retains exact arguments and fixed digest without repeated first chunk',async t=>{
  const f=fixture(t,{chunkSize:70}),plan=await f.plan();
  assert(f.commands.length>2);assert.equal(f.commands.filter(cmd=>cmd.includes("'plan-claim-begin'")).length,1);
  assert(f.commands.slice(1).every(cmd=>cmd.includes("'packet-chunk'")&&!cmd.includes("'--offset' '0'")));
  assert.equal(plan.tool_arguments.requests[0].replaceAllText.replaceText,'exact 😀 文本');
  await f.adapter.io.check(plan,'/private/exact-source');await f.adapter.io.write(plan.tool_arguments);
});
test('Transfer time consumes the original helper deadline rather than starting a new window',async t=>{
  const f=fixture(t,{chunkSize:70,onCommand:(cmd,clock)=>{if(cmd.includes("'packet-chunk'"))clock.advance(2500);}});
  const plan=await f.plan();
  await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/dispatch_window_expired/);
  assert.equal(f.writes.length,0);assert.equal(f.commands.some(cmd=>cmd.includes("'check-cas'")),false);
});
test('Pause after check is caught at the actual connector boundary and burns the local permit',async t=>{
  const f=fixture(t),plan=await f.plan();await f.adapter.io.check(plan,'/private/exact-source');
  f.advance(10000);
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/dispatch_window_expired/);
  f.setClock(100000);
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/permit_unavailable/);assert.equal(f.writes.length,0);
});
test('Observed clock rollback after check is rejected even when still after plan start',async t=>{
  const f=fixture(t),plan=await f.plan();f.advance(5000);
  await f.adapter.io.check(plan,'/private/exact-source');f.advance(-1000);
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/dispatch_window_expired/);assert.equal(f.writes.length,0);
});
test('Mutated argument values before check or write cannot change the authorized CAS',async t=>{
  const f=fixture(t),plan=await f.plan();plan.tool_arguments.write_control.requiredRevisionId='another-revision';
  await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/permit_unavailable/);
  plan.tool_arguments.write_control.requiredRevisionId='exact-revision';await f.adapter.io.check(plan,'/private/exact-source');
  plan.tool_arguments.requests=[];
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/arguments_mismatch/);
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/permit_unavailable/);assert.equal(f.writes.length,0);
});
test('Unplanned arguments, cloned plans, wrong source, and substituted paths cannot authorize a write',async t=>{
  const f=fixture(t),plan=await f.plan();
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/permit_unavailable/);
  await assert.rejects(f.adapter.io.check({...plan},'/private/exact-source'),/permit_unavailable/);
  await assert.rejects(f.adapter.io.check(plan,'/private/other-source'),/permit_unavailable/);
  const original=plan.plan_file;plan.plan_file='/private/other-plan';
  await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/permit_unavailable/);
  plan.plan_file=original;await f.adapter.io.check(plan,'/private/exact-source');
  await assert.rejects(f.adapter.io.write(structuredClone(plan.tool_arguments)),/permit_unavailable/);
  assert.equal(f.writes.length,0);
});
test('Mutating returned proof cannot extend the copied signed deadline',async t=>{
  const f=fixture(t),plan=await f.plan();plan.dispatch_check.execute_before=999999;
  f.advance(10000);await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/dispatch_window_expired/);
  assert.equal(f.writes.length,0);
});
for(const [name,proof] of [['missing clock',{checked_at:undefined}],['nonfinite clock',{checked_at:Infinity}],
  ['missing deadline',{execute_before:undefined}],['mismatched deadline',{execute_before:120}],
  ['nonboolean permission',{dispatch_allowed:1}],['expired proof',{checked_at:110}]])
  test('Malformed bundled check fails closed: '+name,async t=>{
    const f=fixture(t,{proof}),plan=await f.plan();
    await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/dispatch_window_expired/);assert.equal(f.writes.length,0);
  });
for(const [name,mutateChunk] of [['digest change',(p,n)=>n?{...p,sha256:'b'.repeat(64)}:p],
  ['offset gap',p=>({...p,next_offset_chars:p.next_offset_chars+1})],
  ['changed length',(p,n)=>n?{...p,total_chars:p.total_chars+1}:p],
  ['false terminal chunk',p=>({...p,eof:true})]])
  test('Invalid first/continuation result is not retried: '+name,async t=>{
    const f=fixture(t,{chunkSize:70,mutateChunk});await assert.rejects(f.plan(),/global_result_chunk_invalid/);
    assert.equal(f.commands.filter(cmd=>cmd.includes("'plan-claim-begin'")).length,1);assert.equal(f.writes.length,0);
  });
test('Compact heartbeat inspection uses one direct helper RPC with no queue result transfer',async t=>{
  const f=fixture(t);assert.equal(await f.adapter.io.heartbeatDue('/private/exact-source'),false);
  assert.equal(f.commands.length,1);assert.match(f.commands[0],/'inspect-heartbeat'/);
  assert(!f.commands[0].includes('--result-file'));
});
test('Unknown grouped result preserves exact group identity in safe diagnostic evidence',async t=>{
  const f=fixture(t),plan=await f.plan();
  await f.adapter.io.recordFailure(Error('synthetic'),{kind:'claim-begin',stage:'write',plan,
    outcome:'cas_outcome_unknown_no_replay',writeAttempted:true,writeFailure:null});
  assert.deepEqual(f.captures[0].operation_ids,['claim-one','begin-one']);
  assert.equal(f.captures[0].group_id,'group-one');assert.equal(f.captures[0].operation_id,null);
  assert.equal(f.captures[0].retry_allowed,false);
});

test('Spoofed serialization cannot substitute caller-reachable arguments at the real boundary',async t=>{
  const f=fixture(t),plan=await f.plan(),original=structuredClone(plan.tool_arguments);
  plan.tool_arguments.document_id='unapproved-doc';
  plan.tool_arguments.toJSON=()=>original;
  await f.adapter.io.check(plan,'/private/exact-source');await f.adapter.io.write(plan.tool_arguments);
  assert.deepEqual(f.writes,[original]);assert.equal(f.writes[0].document_id,'doc');
});

test('Observed expiry during check permanently burns permit even after clock rollback and recheck',async t=>{
  const f=fixture(t),plan=await f.plan();f.advance(10000);
  await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/dispatch_window_expired/);
  f.setClock(100000);
  await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/permit_unavailable/);
  await assert.rejects(f.adapter.io.write(plan.tool_arguments),/permit_unavailable/);
  assert.equal(f.writes.length,0);
});

for(const [name,options] of [['first chunk',{truncatePlan:true}],['continuation',{truncateContinuation:true}]])
  test('Truncated large '+name+' rereads immutable result with legacy chunks, never replans',async t=>{
    const f=fixture(t,{chunkSize:70,...options}),plan=await f.plan();
    assert.equal(f.commands.filter(c=>c.includes("'plan-claim-begin'")).length,1);
    assert.match(f.commands[0],/'--result-large-chunk'/);
    assert(f.commands.some(c=>c.includes("'packet-chunk'")&&c.includes("'--max-chars' '16384'")));
    await f.adapter.io.check(plan,'/private/exact-source');await f.adapter.io.write(plan.tool_arguments);
    assert.equal(f.writes.length,1);assert.equal(f.writes[0].requests[0].replaceAllText.replaceText,'exact 😀 文本');
  });

test('Legacy fallback transfer still consumes the original dispatch deadline',async t=>{
  const f=fixture(t,{chunkSize:70,truncatePlan:true,onCommand:(cmd,clock)=>{
    if(cmd.includes("'packet-chunk'"))clock.advance(2500);
  }}),plan=await f.plan();
  await assert.rejects(f.adapter.io.check(plan,'/private/exact-source'),/dispatch_window_expired/);
  assert.equal(f.commands.filter(c=>c.includes("'plan-claim-begin'")).length,1);assert.equal(f.writes.length,0);
});
