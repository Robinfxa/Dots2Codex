'use strict';
// Independent adversarial dispatch tests. Synthetic tools; no external effects.
const test=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const {createGlobalControllerToolAdapter}=require('./global_controller_cell');

function fixture(t,{proofChange,afterHelper}={}) {
  let now=2_000_000_000_000;
  const original=Date.now;Date.now=()=>now;t.after(()=>{Date.now=original;});
  const calls=[],commands=[];
  const config={cwd:'/private/package',stateDir:'/private/state',nativeTaskId:'/root/controller',
    documentId:'approved-doc',tabId:'approved-tab',joinCodeFile:'/private/join'};
  const tools={
    async exec_command({cmd}){
      commands.push(cmd);
      const check={dispatch_allowed:true,checked_at:now/1000,execute_before:now/1000+120};
      if(proofChange)proofChange(check);
      const packet={plan_file:'/private/approved-plan',execute_before:now/1000+120,dispatch_check:check,
        tool_arguments:{document_id:'approved-doc',requests:[
          {deleteContentRange:{range:{startIndex:1,endIndex:4,tabId:'approved-tab'}}},
          {insertText:{location:{index:1,tabId:'approved-tab'},text:'approved-text'}}],
          write_control:{requiredRevisionId:'approved-revision'}}};
      const text=JSON.stringify(packet);
      if(afterHelper)afterHelper({advance:seconds=>{now+=seconds*1000;}});
      return {exit_code:0,output:JSON.stringify({text,offset_chars:0,next_offset_chars:[...text].length,
        total_chars:[...text].length,eof:true,sha256:crypto.createHash('sha256').update(text).digest('hex')})};
    },
    async mcp__codex_apps__google_drive_batch_update_document(args){
      calls.push({document_id:args.document_id,requests:args.requests,write_control:args.write_control,
        has_to_json:Object.hasOwn(args,'toJSON')});return {documentId:args.document_id};
    }
  };
  const {io}=createGlobalControllerToolAdapter(tools,config,async()=>'/private/capture');
  return {io,calls,commands,advance:seconds=>{now+=seconds*1000;},
    plan:()=>io.plan('claim-begin','/private/exact-snapshot',{routeId:'a'.repeat(32)})};
}

test('Audit: hidden toJSON cannot substitute different real connector arguments',async t=>{
  const f=fixture(t),plan=await f.plan();
  const exact=JSON.parse(JSON.stringify(plan.tool_arguments));
  plan.tool_arguments.document_id='different-document';
  plan.tool_arguments.requests[1].insertText.text='different-text';
  plan.tool_arguments.toJSON=()=>exact;
  try{await f.io.check(plan,'/private/exact-snapshot');await f.io.write(plan.tool_arguments);}
  catch(error){assert.match(error.message,/dispatch_arguments_mismatch|dispatch_permit_unavailable/);}
  for(const args of f.calls){
    assert.equal(args.document_id,exact.document_id);
    assert.deepEqual(args.requests,exact.requests);
    assert.deepEqual(args.write_control,exact.write_control);
    assert.equal(args.has_to_json,false);
  }
});

test('Audit: delay between check and actual write burns permit without dispatch',async t=>{
  const f=fixture(t),plan=await f.plan();
  await f.io.check(plan,'/private/exact-snapshot');f.advance(120);
  await assert.rejects(f.io.write(plan.tool_arguments),/dispatch_window_expired/);
  f.advance(-120);
  await assert.rejects(f.io.write(plan.tool_arguments),/dispatch_permit_unavailable/);
  assert.deepEqual(f.calls,[]);
});

test('Audit: helper return time counts against original deadline',async t=>{
  const f=fixture(t,{afterHelper:({advance})=>advance(120)}),plan=await f.plan();
  await assert.rejects(f.io.check(plan,'/private/exact-snapshot'),/dispatch_window_expired/);
  await assert.rejects(f.io.write(plan.tool_arguments),/dispatch_permit_unavailable/);
  assert.deepEqual(f.calls,[]);
});

test('Audit: mutable argument changes after check cannot reach connector',async t=>{
  const f=fixture(t),plan=await f.plan();
  await f.io.check(plan,'/private/exact-snapshot');
  plan.tool_arguments.write_control.requiredRevisionId='different-revision';
  await assert.rejects(f.io.write(plan.tool_arguments),/dispatch_arguments_mismatch/);
  plan.tool_arguments.write_control.requiredRevisionId='approved-revision';
  await assert.rejects(f.io.write(plan.tool_arguments),/dispatch_permit_unavailable/);
  assert.deepEqual(f.calls,[]);
});

test('Audit: truthy nonboolean dispatch permission fails closed',async t=>{
  const f=fixture(t,{proofChange:check=>{check.dispatch_allowed=1;}}),plan=await f.plan();
  await assert.rejects(f.io.check(plan,'/private/exact-snapshot'),/dispatch_window_expired/);
  assert.deepEqual(f.calls,[]);
});

test('Audit: exact successful permit is single use and has no separate check RPC',async t=>{
  const f=fixture(t),plan=await f.plan();
  await f.io.check(plan,'/private/exact-snapshot');await f.io.write(plan.tool_arguments);
  await assert.rejects(f.io.check(plan,'/private/exact-snapshot'),/dispatch_permit_unavailable/);
  await assert.rejects(f.io.write(plan.tool_arguments),/dispatch_permit_unavailable/);
  assert.equal(f.calls.length,1);assert.equal(f.commands.length,1);
  assert.match(f.commands[0],/'plan-claim-begin'/);
  assert.match(f.commands[0],/'--check-cas-now'/);
  assert.match(f.commands[0],/'--result-first-chunk'/);
});
