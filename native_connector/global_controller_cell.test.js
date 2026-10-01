'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {createGlobalControllerCell}=require('./global_controller_cell');
function fixture({lost=false,failVerify=false}={}){
  const calls=[];let epoch=0;
  const io={
    async read(){calls.push('read');return {epoch};},
    async plan(kind,source){calls.push('plan:'+kind);return {tool_arguments:{kind},source,kind};},
    async check(){calls.push('check');},
    async write(args){calls.push('write:'+args.kind);epoch++;if(lost)throw Error('lost');return {epoch};},
    async verify(plan,response,readback){calls.push('verify:'+plan.kind);if(failVerify)throw Error('stale');
      assert.equal(readback.epoch,epoch);return {verified:true,first_heartbeat_required:plan.kind==='join'};},
    async nextSecond(){calls.push('nextSecond');}
  };
  return {calls,io,cell:createGlobalControllerCell(io)};
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
  const f=fixture({failVerify:true});await assert.rejects(f.cell.joinAndFirstHeartbeat({}),/stale/);
  assert.equal(f.calls.filter(x=>x==='write:heartbeat').length,0);
});
test('Concurrent invocation fails closed',async()=>{
  const f=fixture();let release;f.io.read=()=>new Promise(resolve=>release=resolve);
  const pending=f.cell.heartbeat();await assert.rejects(f.cell.heartbeat(),/already_running/);
  // finish the pending call with the original asynchronous read.
  f.io.read=async()=>({epoch:1});release({epoch:0});await pending;
});
