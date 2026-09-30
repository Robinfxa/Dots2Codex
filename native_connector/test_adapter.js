'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawnSync}=require('node:child_process');
const {createNativeToolAdapter}=require('./tool_adapter.js');
const {createNativeConnectorRunner}=require('./runner.js');
const cwd=path.resolve(__dirname,'..');
const setup=String.raw`
import json,sys
from pathlib import Path
sys.path.insert(0,'remote_tests')
from test_long_sessions import ConnectorWorkerTests
from remote_transport.model import canonical
f=ConnectorWorkerTests('test_input_is_one_use_after_restart');f.setUp()
f.controller.submit('adapter synthetic','k')
f.cw.accept(*f.execute(f.begin()))
f.cw.input(f.entries_for_all(),1)
f.cw.result(1,'adapter synthetic answer')
manifest=f.cw.root/'base-manifest.json';manifest.write_bytes(canonical(f.entries));manifest.chmod(0o600)
f.tmp._finalizer.detach()
print(json.dumps({'tmp':str(f.root),'root':str(f.cw.root),'manifest':str(manifest),'resource':f.resource()}))
`;
function fixture(){
 const r=spawnSync('python3',['-B','-c',setup],{cwd,encoding:'utf8'});assert.equal(r.status,0,r.stderr);
 return JSON.parse(r.stdout);
}
function fakeTools(f,{badUpload=false}={}){
 let serial=0,active=0,maximum=0,docs=structuredClone(f.resource),commands=0;
 const files=new Map();
 const tools={
  async exec_command(args){
   assert.equal(Object.hasOwn(args,'cwd'),false);commands++;
   const r=spawnSync('bash',['-c',args.cmd],{cwd:args.workdir,encoding:'utf8',maxBuffer:10*1024*1024});
   // Emulate a strict output wrapper ceiling. Oversized helper data would fail.
   if(r.stdout.length>20000)throw Error('synthetic_output_truncated');
   return {exit_code:r.status,output:r.stdout,wall_time_seconds:0};
  },
  async mcp__codex_apps__google_drive_upload_file(args){
   const id='fake-'+(++serial),file={id,raw:fs.readFileSync(args.file_uri),title:args.file_name,
    mime_type:args.mime_type,parent_ids:[args.parent_folder_id],url:'https://drive.google.com/file/d/'+id+'/view'};
   files.set(id,file);maximum=Math.max(maximum,++active);
   await new Promise(resolve=>setTimeout(resolve,5));active--;
   return {isError:badUpload,structuredContent:{success:true,id}};
  },
  async mcp__codex_apps__google_drive_get_file_metadata({fileId}){
   const {raw,...meta}=files.get(fileId);return {structuredContent:meta};
  },
  async mcp__codex_apps__google_drive_fetch({url,download_raw_file,include_base64}){
   assert.equal(download_raw_file,true);assert.equal(include_base64,false);
   const id=/\/d\/([^/]+)/.exec(url)[1];assert.ok(files.has(id));
   return {structuredContent:{id,file_uri:{file_id:'sediment://file_'+id}}};
  },
  async download_file({file_id}){
   const id=file_id.slice('file_'.length),file=path.join(f.tmp,file_id+'.json');
   fs.writeFileSync(file,files.get(id).raw,{mode:0o600});return {path:file,size_bytes:files.get(id).raw.length};
  },
  async mcp__codex_apps__google_drive_get_document(){return {structuredContent:structuredClone(docs)};},
  async mcp__codex_apps__google_drive_batch_update_document(args){
   assert.equal(args.write_control.requiredRevisionId,docs.revisionId);
   const r=args.requests[0].replaceAllText;
   const text=docs.tabs[0].documentTab.body.content[1].paragraph.elements[0].textRun;
   assert.equal(text.content.includes(r.containsText.text),true);
   text.content=text.content.replace(r.containsText.text,r.replaceText);
   docs.revisionId='opaque-adapter-next';
   return {structuredContent:{documentId:'doc',writeControl:{requiredRevisionId:docs.revisionId},
    replies:[{replaceAllText:{occurrencesChanged:1}}]}};
  }
 };
 return {tools,stats:()=>({maximum,commands,files:files.size})};
}
test('actual adapter + real Python CLI: parallel exact uploads and result CAS',async()=>{
 const f=fixture();try{
  const fake=fakeTools(f),adapter=createNativeToolAdapter(fake.tools,{cwd,root:f.root,nativeTaskId:'synthetic/native',
   documentId:'doc',manifest:f.manifest});
  const runner=createNativeConnectorRunner(adapter.io,{now:adapter.now});
  const result=await runner.uploadAndCommit({seq:1});assert.equal(result.ok,true,JSON.stringify(result));
  const state=JSON.parse(fs.readFileSync(path.join(f.root,'worker.json')));
  assert.equal(state.records['1'].phase,'result_committed');assert.equal(fake.stats().files,3);
  assert.equal(fake.stats().maximum,3);
  const retry=await runner.uploadVerifiedBatch({seq:1});assert.equal(retry.ok,false);assert.equal(fake.stats().files,3);
 }finally{fs.rmSync(f.tmp,{recursive:true,force:true});}
});
test('returned error upload captures retain original object association',async()=>{
 const f=fixture();try{
  const fake=fakeTools(f,{badUpload:true}),adapter=createNativeToolAdapter(fake.tools,{cwd,root:f.root,
   nativeTaskId:'synthetic/native',documentId:'doc',manifest:f.manifest});
  const runner=createNativeConnectorRunner(adapter.io,{now:()=>performance.now()});
  const result=await runner.uploadVerifiedBatch({seq:1});assert.equal(result.ok,false);
  const envelopes=fs.readdirSync(f.root).filter(n=>n.startsWith('capture-')).map(n=>JSON.parse(fs.readFileSync(path.join(f.root,n))))
   .filter(v=>v.stage==='upload');
  assert.equal(envelopes.length,3);assert.equal(new Set(envelopes.map(v=>v.context.object_id)).size,3);
  assert.ok(envelopes.every(v=>v.context.batch_id && v.response.isError && v.response.structuredContent.id));
 }finally{fs.rmSync(f.tmp,{recursive:true,force:true});}
});
test('quote-heavy/null/unicode large captures are lossless and argv bounded',async()=>{
 const f=fixture();try{
  const fake=fakeTools(f),adapter=createNativeToolAdapter(fake.tools,{cwd,root:f.root,nativeTaskId:'synthetic/native',documentId:'doc'});
  const value={a:null,b:"'".repeat(40000)+'😀'.repeat(10000),c:false};
  const saved=await adapter.captureValue(value);
  assert.deepEqual(JSON.parse(fs.readFileSync(saved)),value);
  const mono=await adapter.now();assert.ok(Number.isFinite(mono));
 }finally{fs.rmSync(f.tmp,{recursive:true,force:true});}
});

test('large helper packets never rely on stdout output cap',async()=>{
 const f=fixture();try{
  const fake=fakeTools(f),adapter=createNativeToolAdapter(fake.tools,{cwd,root:f.root,nativeTaskId:'synthetic/native',documentId:'doc'});
  const p=path.join(f.root,'worker.json'),state=JSON.parse(fs.readFileSync(p));
  state.records['fixture-large-status']={padding:'字😀'.repeat(30000)};
  fs.writeFileSync(p,JSON.stringify(state),{mode:0o600});
  const actual=await adapter.worker('status');
  assert.equal(actual.records['fixture-large-status'].padding,state.records['fixture-large-status'].padding);
  assert.ok(fake.stats().commands>10);
 }finally{fs.rmSync(f.tmp,{recursive:true,force:true});}
});
