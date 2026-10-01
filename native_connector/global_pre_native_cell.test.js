'use strict';
// Synthetic boundary tests only. These tools cannot dispatch native work.
const test=require('node:test');
const assert=require('node:assert/strict');
const {createGlobalControllerToolAdapter}=require('./global_controller_cell');

function fixture(t,{chunkSize=100000,proof={},mutatePacket=()=>{},onCommand=()=>{},mutateChunk=x=>x,nativePlanFile=undefined}={}) {
  let clock=100000,packet=null,chunkCount=0,reads=0;
  const commands=[],captures=[],calls=[];
  t.mock.method(Date,'now',()=>clock);
  const config={cwd:'/private/package',stateDir:'/private/state',nativeTaskId:'/root/controller',
    documentId:'doc',tabId:'tab',joinCodeFile:'/private/join',nativePlanFile};
  const argumentsValue={task_name:'worker_one',message:'Private handoff reference only 😀',fork_turns:'none'};
  function chunk(offset) {
    const chars=Array.from(JSON.stringify(packet)),end=Math.min(offset+chunkSize,chars.length);
    return mutateChunk({offset_chars:offset,next_offset_chars:end,total_chars:chars.length,
      text:chars.slice(offset,end).join(''),sha256:'a'.repeat(64),eof:end===chars.length},chunkCount++);
  }
  const tools={
    async exec_command({cmd}) {
      calls.push('helper');commands.push(cmd);onCommand(cmd,{advance:ms=>{clock+=ms;}});
      let value;
      if(cmd.includes("'packet-chunk'")) {
        assert.match(cmd,/'--sha256' 'a{64}'/);
        value=chunk(Number(cmd.match(/'--offset' '(\d+)'/)[1]));
      }else{
        assert.match(cmd,/'plan-native'/);assert.match(cmd,/'--check-native-now'/);
        assert.match(cmd,/'--package-root' '\/private\/package'/);
        assert.match(cmd,/'--route-id' 'route-one'/);
        assert.match(cmd,/'--inline-evidence'/);
        packet={plan_file:'/private/native-plan',tool:'collaboration.spawn_agent',arguments:argumentsValue,
          execute_before:110,dispatch_check:{dispatch_allowed:true,checked_at:100,execute_before:110,...proof},
          expected_state:{private:'must not be returned'},unused:'not caller authority'};
        packet.snapshot_file='/private/source-'+reads;mutatePacket(packet);value=chunk(0);
      }
      return {exit_code:0,output:JSON.stringify(value)};
    },
    async mcp__codex_apps__google_drive_get_document(){calls.push('read');return {documentId:'doc',revisionId:'r'+(++reads),tabs:[]};},
    async mcp__codex_apps__google_drive_batch_update_document(){throw Error('no provider write allowed in native preparation');},
    async collaboration_spawn_agent(){throw Error('no native dispatch allowed in JS');}
  };
  const adapter=createGlobalControllerToolAdapter(tools,config,async value=>{
    calls.push('capture');captures.push(value);return '/private/source-'+captures.length;});
  return {adapter,commands,captures,calls,argumentsValue,prepare:()=>adapter.cell.prepareNative({routeId:'route-one'})};
}

test('Pre-native preparation reads fresh full authority and exposes checked exact args without native dispatch',async t=>{
  const f=fixture(t),result=await f.prepare();
  assert.deepEqual(f.calls,['read','helper']);assert.equal(f.commands.length,1);
  assert.equal(result.tool,'collaboration.spawn_agent');assert.deepEqual(result.arguments,f.argumentsValue);
  assert.equal(result.plan_file,'/private/native-plan');assert.equal(result.record_snapshot_file,'/private/source-1');
  assert.equal(result.execute_before,110);assert.equal(result.checked_at,100);
  assert.equal(result.route_id,'route-one');assert.equal(result.one_attempt_only,true);assert.equal(result.retry_allowed,false);
  assert.equal(result.next_direct_platform_tool_required,true);assert.equal(result.native_spawn_invoked_by_cell,false);
  assert.equal(Object.hasOwn(result,'expected_state'),false);assert.equal(Object.hasOwn(result,'unused'),false);
  assert.equal(Object.hasOwn(result,'dispatch_check'),false);
});

test('Native preparation never caches a prior full read even in the same cell instance',async t=>{
  const f=fixture(t);await f.prepare();await f.prepare();
  assert.equal(f.captures.length,0);
  assert.match(f.commands[0],/revisionId[^,]+r1/);assert.match(f.commands[1],/revisionId[^,]+r2/);
  // The real durable ledger rejects the repeated route reservation. This test
  // isolates only the absence of cached remote authority at the adapter layer.
});

test('Native argument chunk transfer remains digest-bound and byte-exact',async t=>{
  const f=fixture(t,{chunkSize:70}),result=await f.prepare();
  assert(f.commands.length>2);assert.equal(f.commands.filter(c=>c.includes("'plan-native'")).length,1);
  assert.deepEqual(result.arguments,f.argumentsValue);
  assert(f.commands.slice(1).every(c=>c.includes("'packet-chunk'")&&!c.includes("'--offset' '0'")));
});

test('Native result transfer consumes the original ten-second window and cannot replan on expiry',async t=>{
  const f=fixture(t,{chunkSize:70,onCommand:(cmd,clock)=>{if(cmd.includes("'packet-chunk'"))clock.advance(2000);}});
  await assert.rejects(f.prepare(),/global_native_dispatch_window_expired_no_replay/);
  assert.equal(f.commands.filter(c=>c.includes("'plan-native'")).length,1);
});

test('Native preparation observed rollback before output fails closed',async t=>{
  const f=fixture(t,{onCommand:(_,clock)=>clock.advance(-1)});
  await assert.rejects(f.prepare(),/global_native_dispatch_window_expired_no_replay/);
});

for(const [name,proof] of [['missing checked clock',{checked_at:undefined}],['missing deadline',{execute_before:undefined}],
  ['wrong deadline',{execute_before:120}],['nonboolean permission',{dispatch_allowed:1}],['already expired',{checked_at:110}]])
  test('Native preparation rejects malformed bundled proof: '+name,async t=>{
    const f=fixture(t,{proof});await assert.rejects(f.prepare(),/global_native_dispatch_window_expired_no_replay/);
    assert.equal(f.commands.length,1);
  });

for(const [name,mutatePacket] of [['wrong native tool',p=>{p.tool='unapproved';}],['missing plan',p=>{delete p.plan_file;}],
  ['array arguments',p=>{p.arguments=[];}],['missing args',p=>{delete p.arguments;}]])
  test('Native preparation rejects malformed exact packet: '+name,async t=>{
    const f=fixture(t,{mutatePacket});await assert.rejects(f.prepare(),/global_native_preparation_unverified/);
  });

test('Native preparation rejects a changed continuation digest without another planning attempt',async t=>{
  const f=fixture(t,{chunkSize:70,mutateChunk:(part,index)=>index?{...part,sha256:'b'.repeat(64)}:part});
  await assert.rejects(f.prepare(),/global_result_chunk_invalid/);
  assert.equal(f.commands.filter(c=>c.includes("'plan-native'")).length,1);
});

test('Preselected native plan path is used exactly so post-spawn cell can be emitted ahead of dispatch',async t=>{
  const nativePlanFile='/private/state/preselected-native-plan.json',f=fixture(t,{nativePlanFile});
  await f.prepare();assert.match(f.commands[0],/'--save' '\/private\/state\/preselected-native-plan.json'/);
});

test('Invalid optional preselected plan path fails before any tool can reserve native',async t=>{
  for(const nativePlanFile of [null,'',{},[]])assert.throws(()=>fixture(t,{nativePlanFile}),/global_cell_config_required/);
});
