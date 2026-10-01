'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
const {createGlobalControllerToolAdapter}=require('./global_controller_cell');

test('Audit: an observed expired Global dispatch check cannot revive on a second check',async t=>{
  let now=2_000_000_000_000;
  t.mock.method(Date,'now',()=>now);
  const calls=[];
  const config={cwd:'/private/package',stateDir:'/private/state',nativeTaskId:'/root/controller',
    documentId:'approved-doc',tabId:'approved-tab',joinCodeFile:'/private/code'};
  const tools={
    async exec_command(){
      const packet={plan_file:'/private/plan',execute_before:now/1000+120,
        dispatch_check:{dispatch_allowed:true,checked_at:now/1000,execute_before:now/1000+120},
        tool_arguments:{document_id:'approved-doc',requests:[],write_control:{requiredRevisionId:'r1'}}};
      const text=JSON.stringify(packet);
      return {exit_code:0,output:JSON.stringify({text,offset_chars:0,next_offset_chars:[...text].length,
        total_chars:[...text].length,eof:true,sha256:crypto.createHash('sha256').update(text).digest('hex')})};
    },
    async mcp__codex_apps__google_drive_batch_update_document(args){calls.push(args);return {documentId:args.document_id};}
  };
  const {io}=createGlobalControllerToolAdapter(tools,config,async()=>'/private/capture');
  const plan=await io.plan('claim-begin','/private/exact-source',{routeId:'a'.repeat(32)});
  now+=120000;
  await assert.rejects(io.check(plan,'/private/exact-source'),/dispatch_window_expired/);
  now-=120000;
  await assert.rejects(io.check(plan,'/private/exact-source'));
  await assert.rejects(io.write(plan.tool_arguments));
  assert.equal(calls.length,0);
});
