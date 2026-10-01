'use strict';
// Independent exact-argument/final-dispatch audit. Synthetic tools only.
const test=require('node:test');
const assert=require('node:assert/strict');
const crypto=require('node:crypto');
global.createNativeToolAdapter=require('./tool_adapter').createNativeToolAdapter;
const {createRouterPairingToolAdapter}=require('./router_pairing');

function fixture(t,{proofChange,afterReserve,afterPrepare,chunkSize=100000,truncateContinuation=false}={}) {
  let now=2_000_000_000_000;
  const original=Date.now;Date.now=()=>now;t.after(()=>{Date.now=original;});
  const config={cwd:'/private/package',stateDir:'/private/child',nativeTaskId:'/root/child',
    documentId:'approved-doc',tabId:'approved-tab',controlDocumentId:'approved-control',
    handoff:{path:'/private/handoff',sha256:'a'.repeat(64)},admissionReceipt:'/private/receipt',
    expires:now/1000+120};
  const calls=[],uploads=[],commands=[];
  let packetText,packetDigest,resultFile,continuationTruncated=false;
  function part(offset) {
    const chars=[...packetText],end=Math.min(offset+chunkSize,chars.length);
    return {text:chars.slice(offset,end).join(''),offset_chars:offset,next_offset_chars:end,
      total_chars:chars.length,eof:end===chars.length,sha256:packetDigest};
  }
  const argumentsValue={document_id:'approved-doc',requests:[{replaceAllText:{replaceText:'approved-state'}}],
    write_control:{requiredRevisionId:'approved-revision'}};
  const tools={
    async exec_command({cmd}) {
      commands.push(cmd);
      let value;
      if(cmd.includes("'plan-admit'")) {
        const dispatch_check={reserved:true,operation_id:'a'.repeat(32),checked_at:now/1000,
          execute_before:config.expires,one_attempt_only:true};
        if(proofChange)proofChange(dispatch_check);
        packetText=JSON.stringify({plan_file:'/private/approved-plan',stage:'WORKER_ADMITTED',
          operation_id:'a'.repeat(32),tool_arguments:argumentsValue,dispatch_check});
        packetDigest=crypto.createHash('sha256').update(packetText).digest('hex');
        resultFile=cmd.match(/'--result-file' '([^']+)'/)[1];value=part(0);
        if(afterReserve)afterReserve(seconds=>{now+=seconds*1000;});
      }else if(cmd.includes("'packet-chunk'")){
        assert.equal(cmd.match(/'--path' '([^']+)'/)[1],resultFile);
        assert.equal(cmd.match(/'--sha256' '([^']+)'/)[1],packetDigest);
        const offset=Number(cmd.match(/'--offset' '(\d+)'/)[1]);
        if(truncateContinuation&&!continuationTruncated){
          continuationTruncated=true;return {exit_code:0,output:'{"truncated":'};
        }
        value=part(offset);
      }else if(cmd.includes("'reserve-write'")){
        value={reserved:true,operation_id:'a'.repeat(32),checked_at:now/1000,
          execute_before:config.expires,one_attempt_only:true};
        if(proofChange)proofChange(value);
        if(afterReserve)afterReserve(seconds=>{now+=seconds*1000;});
      }else if(cmd.includes("'prepare'")){
        value={probe:{probe_file:'/private/exact-probe',file_name:'approved-probe.json',
          mime_type:'application/json',folder_id:'approved-folder'},
          forward_probe:{file_id:'approved-forward'},folder_id:'approved-folder',one_attempt_only:true,
          checked_at:now/1000,execute_before:config.expires};
        if(afterPrepare)afterPrepare(seconds=>{now+=seconds*1000;});
      }
      else if(cmd.includes("'capture'"))value={path:'/private/captured'};
      else throw Error('unexpected synthetic command');
      return {exit_code:0,output:JSON.stringify(value)};
    },
    async mcp__codex_apps__google_drive_batch_update_document(args) {
      calls.push(structuredClone(args));return {documentId:args.document_id};
    },
    async mcp__codex_apps__google_drive_upload_file(args) {
      uploads.push(structuredClone(args));return {success:true,id:'approved-upload'};
    },
    async mcp__codex_apps__google_drive_get_file_metadata(){throw Error('synthetic readback failure');}
  };
  const {io}=createRouterPairingToolAdapter(tools,config);
  return {io,calls,uploads,commands,config,advance:seconds=>{now+=seconds*1000;},
    plan:()=>io.planAdmit('/private/snapshot',{metadata:'/forward/meta',raw:'/forward/raw',
      fetch:'/forward/fetch',download:'/forward/download'},
      {upload:'/reverse/upload',metadata:'/reverse/meta',raw:'/reverse/raw',
        fetch:'/reverse/fetch',download:'/reverse/download'})};
}

test('Audit: child CAS requires its exact reserved one-use plan',async t=>{
  const f=fixture(t),plan=await f.plan();
  await assert.rejects(f.io.casWrite(plan));
  assert.equal(f.calls.length,0);
});

test('Audit: child CAS mutation after reservation cannot change actual connector args',async t=>{
  const f=fixture(t),plan=await f.plan();await f.io.reserveWrite(plan);
  plan.tool_arguments.document_id='unapproved-doc';
  await assert.rejects(f.io.casWrite(plan));
  assert.equal(f.calls.length,0);
});

test('Audit: child CAS accepts one exact dispatch and no replay',async t=>{
  const f=fixture(t),plan=await f.plan();await f.io.reserveWrite(plan);await f.io.casWrite(plan);
  await assert.rejects(f.io.casWrite(plan));
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].document_id,'approved-doc');
});

test('Audit: child CAS dispatch cannot outlive its original helper deadline',async t=>{
  const f=fixture(t),plan=await f.plan();await f.io.reserveWrite(plan);f.advance(120);
  await assert.rejects(f.io.casWrite(plan));
  f.advance(-120);await assert.rejects(f.io.casWrite(plan));
  assert.equal(f.calls.length,0);
});

test('Audit: failed reservation proof never grants child CAS permission',async t=>{
  const f=fixture(t,{proofChange:proof=>{proof.reserved=false;}}),plan=await f.plan();
  await assert.rejects(f.io.reserveWrite(plan));
  await assert.rejects(f.io.casWrite(plan));
  assert.equal(f.calls.length,0);
});

test('Audit: expired reservation return cannot be revived by clock rollback',async t=>{
  const f=fixture(t,{afterReserve:advance=>advance(120)}),plan=await f.plan();
  await assert.rejects(f.io.reserveWrite(plan));
  f.advance(-120);await assert.rejects(f.io.casWrite(plan));
  assert.equal(f.calls.length,0);
});

test('Audit: own toJSON cannot substitute unapproved child CAS values',async t=>{
  const f=fixture(t),plan=await f.plan();await f.io.reserveWrite(plan);
  const original=JSON.parse(JSON.stringify(plan.tool_arguments));
  plan.tool_arguments.document_id='unapproved-doc';plan.tool_arguments.toJSON=()=>original;
  await assert.rejects(f.io.casWrite(plan));assert.equal(f.calls.length,0);
});

test('Audit: probe upload path and destination are exact and one use',async t=>{
  const f=fixture(t),prepared=await f.io.prepare('/private/snapshot');
  prepared.probe.folder_id='unapproved-folder';
  await assert.rejects(f.io.reverseProbe(prepared));
  prepared.probe.folder_id='approved-folder';await assert.rejects(f.io.reverseProbe(prepared));
  assert.equal(f.uploads.length,0);
});

test('Audit: actual reverse upload is never replayed after downstream readback fails',async t=>{
  const f=fixture(t),prepared=await f.io.prepare('/private/snapshot');
  await assert.rejects(f.io.reverseProbe(prepared));
  await assert.rejects(f.io.reverseProbe(prepared));
  assert.equal(f.uploads.length,1);assert.equal(f.uploads[0].parent_folder_id,'approved-folder');
});

test('Audit: late prepare helper exposes no probe upload permit',async t=>{
  const f=fixture(t,{afterPrepare:advance=>advance(120)});
  await assert.rejects(f.io.prepare('/private/snapshot'));
  assert.equal(f.uploads.length,0);
});

test('Audit: truncated child continuation resumes same immutable offset and digest without replanning',async t=>{
  const f=fixture(t,{chunkSize:80,truncateContinuation:true}),plan=await f.plan();
  await f.io.reserveWrite(plan);await f.io.casWrite(plan);
  assert.equal(f.commands.filter(cmd=>cmd.includes("'plan-admit'")).length,1);
  const chunks=f.commands.filter(cmd=>cmd.includes("'packet-chunk'"));assert(chunks.length>2);
  assert.match(chunks[0],/'--max-chars' '65536'/);
  assert.match(chunks[1],/'--max-chars' '16384'/);
  assert.equal(chunks[0].match(/'--offset' '(\d+)'/)[1],chunks[1].match(/'--offset' '(\d+)'/)[1]);
  assert.equal(chunks[0].match(/'--sha256' '([^']+)'/)[1],chunks[1].match(/'--sha256' '([^']+)'/)[1]);
  assert(chunks.slice(1).every(cmd=>cmd.includes("'--max-chars' '16384'")));
  assert.equal(f.calls.length,1);
});
