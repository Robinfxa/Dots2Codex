'use strict';
// Inline evidence stays private, bounded and exact. No real provider/native tools.
const test=require('node:test');
const assert=require('node:assert/strict');
const {createGlobalControllerToolAdapter}=require('./global_controller_cell');

function inputOf(cmd){
  const prefix="printf '%s' ",end=cmd.lastIndexOf(" | 'python3'");
  assert(cmd.startsWith(prefix)&&end>prefix.length);
  const quoted=cmd.slice(prefix.length,end);assert(quoted.startsWith("'")&&quoted.endsWith("'"));
  return JSON.parse(quoted.slice(1,-1).replace(/'\\''/g,"'"));
}
function fixture({content='exact 😀 data',response={documentId:'doc'},missingPath=false}={}){
  const commands=[],captures=[],inputs=[],writes=[];let reads=0;
  const tools={
    async mcp__codex_apps__google_drive_get_document(){return {structuredContent:{documentId:'doc',
      revisionId:'r'+(++reads),tabs:[{content}]}};},
    async mcp__codex_apps__google_drive_batch_update_document(args){writes.push(args);return response;},
    async exec_command({cmd}){
      commands.push(cmd);const inline=cmd.includes("'--inline-evidence'");
      if(inline)inputs.push(inputOf(cmd));
      const now=Date.now()/1000;
      let value=cmd.includes("'verify'")?{verified:true,first_heartbeat_required:false,heartbeat_due_at:now+25}:
        {plan_file:'/private/plan',execute_before:now+100,dispatch_check:{dispatch_allowed:true,
          checked_at:now,execute_before:now+100},tool_arguments:{document_id:'doc',write_control:{requiredRevisionId:'r1'}}};
      if(inline&&!missingPath)value={...value,snapshot_file:'/private/inline-'+commands.length,response_file:'/private/response'};
      const text=JSON.stringify(value);
      return {exit_code:0,output:cmd.includes("'--result-first-chunk'")?JSON.stringify({offset_chars:0,text,
        sha256:'a'.repeat(64),next_offset_chars:[...text].length,total_chars:[...text].length,eof:true}):text};
    }
  };
  const adapter=createGlobalControllerToolAdapter(tools,{cwd:'/private/package',stateDir:'/private/state',
    nativeTaskId:'/root/controller',documentId:'doc',tabId:'tab',joinCodeFile:'/private/join'},async value=>{
      captures.push(value);return '/private/fallback-'+captures.length;});
  return {adapter,commands,captures,inputs,writes,get reads(){return reads;}};
}

test('Exact snapshot and actual response share bounded capture+plan and capture+verify RPCs',async()=>{
  const response={documentId:'doc',replies:[{exact:'actual reply 😀'}]},f=fixture({response});
  const result=await f.adapter.cell.heartbeat();
  assert.equal(result.verified,true);assert.equal(Object.hasOwn(result,'snapshot_file'),false);
  assert.equal(Object.hasOwn(result,'response_file'),false);
  assert.equal(f.commands.length,2);assert.equal(f.captures.length,0);assert.equal(f.reads,2);assert.equal(f.writes.length,1);
  assert.deepEqual(f.inputs[0],{snapshot:{documentId:'doc',revisionId:'r1',tabs:[{content:'exact 😀 data'}]},response:null});
  assert.deepEqual(f.inputs[1],{snapshot:{documentId:'doc',revisionId:'r2',tabs:[{content:'exact 😀 data'}]},response});
  assert(!f.commands.some(c=>c.includes("'--snapshot'")||c.includes("'--readback'")||c.includes("'--response'")));
});

test('Shell metacharacters, apostrophes and Unicode survive quoting exactly as private stdin data',async()=>{
  const content="'; $(touch /tmp/NOT_EXECUTED) && `echo bad` \\ \n 😀 中文",f=fixture({content});
  await f.adapter.cell.heartbeat();
  assert.equal(f.inputs.length,2);assert.equal(f.inputs[0].snapshot.tabs[0].content,content);
  assert(f.commands[0].includes("'\\''"));assert.equal(f.captures.length,0);
});

for(const [label,content] of [['quote amplification',"'".repeat(15000)],['UTF-8 bytes','😀'.repeat(16000)]])
  test('Large '+label+' safely falls back to exact file captures',async()=>{
    const f=fixture({content});await f.adapter.cell.heartbeat();
    assert.equal(f.inputs.length,0);assert.equal(f.captures.length,3);
    assert.equal(f.captures[0].tabs[0].content,content);assert.equal(f.captures[1].tabs[0].content,content);
    assert.deepEqual(f.captures[2],{documentId:'doc'});
    assert.match(f.commands[0],/'--snapshot' '\/private\/fallback-1'/);
    assert.match(f.commands[1],/'--readback' '\/private\/fallback-2'/);
    assert.match(f.commands[1],/'--response' '\/private\/fallback-3'/);
  });

test('Oversized actual response falls back for full readback and response without trimming',async()=>{
  const response={documentId:'doc',large:'x'.repeat(65000)},f=fixture({response});
  await f.adapter.cell.heartbeat();assert.equal(f.inputs.length,1);assert.equal(f.captures.length,2);
  assert.equal(f.captures[0].revisionId,'r2');assert.deepEqual(f.captures[1],response);
});

test('Read handle is opaque and never substitutes a copied cached revision for another read',async()=>{
  const f=fixture(),source=await f.adapter.io.read();assert(Object.isFrozen(source));assert.deepEqual(Object.keys(source),[]);
  const plan=await f.adapter.io.plan('heartbeat',source,{});
  await f.adapter.io.check(plan,source);await f.adapter.io.write(plan.tool_arguments);
  const readback=await f.adapter.io.read();assert.notEqual(source,readback);
  await f.adapter.io.verify(plan,{documentId:'doc'},readback);
  assert.equal(f.inputs[0].snapshot.revisionId,'r1');assert.equal(f.inputs[1].snapshot.revisionId,'r2');
});

test('Missing inline durable capture acknowledgement cannot authorize a CAS',async()=>{
  const f=fixture({missingPath:true});await assert.rejects(f.adapter.cell.heartbeat(),/global_local_helper_output_invalid/);
  assert.equal(f.commands.length,1);assert.equal(f.writes.length,0);
  assert.equal(f.captures.length,1);assert.equal(f.captures[0].contract,'dots-global-controller-diagnostic/1');
});
