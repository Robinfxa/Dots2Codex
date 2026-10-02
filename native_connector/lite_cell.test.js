'use strict';
const test=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'), path=require('node:path'), os=require('node:os');
const {spawnSync}=require('node:child_process');
const {createLiteNativeAdapter,createLiteMemoryCaptureSink}=require('./lite_cell.js');
const original=path.resolve(__dirname,'..');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const goldenRaw=JSON.parse(fs.readFileSync(path.join(original,'lite_tests/fixtures/normalized-raw-fetch.json')));
const goldenMetadata=JSON.parse(fs.readFileSync(path.join(original,'lite_tests/fixtures/normalized-file-metadata.json')));
function command(cwd,args){const r=spawnSync('python3',['-B',...args],{cwd,encoding:'utf8',maxBuffer:4*1024*1024});assert.equal(r.status,0,r.stderr);return JSON.parse(r.stdout);}
function privateJSON(file,value){fs.writeFileSync(file,JSON.stringify(value),{mode:0o600});return file;}
function canonical(value){if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';return JSON.stringify(value);}
const setup=String.raw`
import json,os,secrets,sys,time
from pathlib import Path
from dots_lite.protocol import *
from dots_lite.package_identity import verify_package
from dots_lite.authorization import authorization_scope
from dots_lite.private_io import save
root=Path(sys.argv[1]);now=int(time.time());key=secrets.token_hex(32)
grant={'protocol':PROTOCOL,'activation_id':'activation-test','folder_id':'fixture-folder','inbox_id':'inbox-test','created_at':now-1,'expires_at':now+3600,'allowed_pairs':[{'model':'gpt-6.1-sol','reasoning_effort':'xhigh'}],'limits':dict(DEFAULT_LIMITS),'package_sha256':verify_package()}
routes=[];files={}
for rid in ('alpha','beta'):
 body={'model':'gpt-6.1-sol','reasoning':{'effort':'xhigh'},'stream':True,'input':[{'role':'user','content':'Unicode 😀 private payload '+rid}],'tools':json.loads(sys.argv[2])}
 raw=canonical(body);fid='request-'+rid
 descriptor={'seq':1,'request_id':'request-id-'+rid,'request_sha256':sha256(raw),'byte_length':len(raw),'file_id':fid,'folder_id':'fixture-folder','previous_result_ack':None,'begin_before':now+3500}
 routes.append({'route_id':rid,'identity_sha256':sha256(rid.encode()),'model':'gpt-6.1-sol','reasoning_effort':'xhigh','outbox_id':'outbox-'+rid,'request':descriptor,'stop':False})
 files[fid]=raw.decode()
inbox=make_inbox(grant,routes,key,'inbox-operation')
join={'activation_id':grant['activation_id'],'inbox_id':grant['inbox_id'],'grant_sha256':grant_hash(grant),'join_code':key,'transport_authorization':authorization_scope(grant)}
(root/'join.txt').write_text(JOIN_MARKER+' '+canonical(join).decode());os.chmod(root/'join.txt',0o600)
print(json.dumps({'inbox':inbox,'files':files,'routes':routes,'grant':grant}))
`;
function fixture(requestTools=[],{temporaryRoot=os.tmpdir()}={}){
 const tmp=fs.mkdtempSync(path.join(temporaryRoot,'dots-lite-adapter-'));fs.chmodSync(tmp,0o700);
 const cwd=path.join(tmp,'package');fs.mkdirSync(cwd,{mode:0o700});
 fs.cpSync(path.join(original,'dots_lite'),path.join(cwd,'dots_lite'),{recursive:true,filter:x=>!x.includes('__pycache__')});
 fs.mkdirSync(path.join(cwd,'native_connector'));fs.copyFileSync(path.join(original,'native_connector/lite_cell.js'),path.join(cwd,'native_connector/lite_cell.js'));
 fs.copyFileSync(path.join(original,'LIGHTWEIGHT.command'),path.join(cwd,'LIGHTWEIGHT.command'));
 const crypto=require('node:crypto'),files=[];
 for(const name of fs.readdirSync(path.join(cwd,'dots_lite')).filter(n=>n.endsWith('.py')))files.push('dots_lite/'+name);
 files.push('native_connector/lite_cell.js','LIGHTWEIGHT.command');files.sort();
 privateJSON(path.join(cwd,'LIGHTWEIGHT_PACKAGE_MANIFEST.json'),{contract:'dots-lite-package/3',files:files.map(p=>({path:p,sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(cwd,p))).digest('hex')}))});
 const seed=command(cwd,['-c',setup,tmp,JSON.stringify(requestTools)]);const root=path.join(tmp,'parent');
 const init=command(cwd,['-m','dots_lite.cli','init-parent','--state-dir',root,'--join-file',path.join(tmp,'join.txt'),'--actor-task-id','/root','--authorization-message-id','synthetic-user-message','--available-child-slots','3']);assert.equal(init.ok,true,JSON.stringify(init));
 return {tmp,cwd,root,...seed,cleanup(){fs.rmSync(tmp,{recursive:true,force:true});}};
}
function resource(id,text='\n',revision='revision-0'){
 const content=[{endIndex:1,sectionBreak:{}}];let index=1;
 for(const line of text.match(/[^\n]*\n/g)||[]){content.push({startIndex:index,endIndex:index+line.length,paragraph:{elements:[{startIndex:index,endIndex:index+line.length,textRun:{content:line,textStyle:{}}}],paragraphStyle:{}}});index+=line.length;}
 return {documentId:id,revisionId:revision,suggestionsViewMode:'SUGGESTIONS_INLINE',body:null,tabs:[{tabId:'tab-'+id,title:'Control',index:0,nestingLevel:null,parentTabId:null,body:{content}}]};
}
function docText(doc){return doc.tabs[0].body.content.filter(x=>x.paragraph).flatMap(x=>x.paragraph.elements).map(x=>x.textRun.content).join('');}
function fakeTools(f,options={}){
 const docs=new Map([['inbox-test',resource('inbox-test',canonical(f.inbox)+'\n')],...f.routes.map(r=>[r.outbox_id,resource(r.outbox_id)])]);
 const blobs=new Map(Object.entries(f.files).map(([id,value])=>[id,Buffer.from(value)]));
 const counters={exec:0,read:0,write:0,metadata:0,fetch:0,download:0,upload:0}, effects=[];let serial=0;
 const privateKey=JSON.parse(fs.readFileSync(path.join(f.root,'native-config.json'))).join.join_code;
 const tools={
  async exec_command(args){counters.exec++;assert.equal(args.cmd.includes(privateKey),false);
   const encoded=/'--input-base64' '([^']*)'/.exec(args.cmd);if(encoded){assert.equal(Buffer.from(encoded[1],'base64').toString().includes(privateKey),false);assert.equal(args.max_output_tokens,32768);}
   const r=spawnSync('bash',['-c',args.cmd],{cwd:args.workdir,encoding:'utf8',maxBuffer:4*1024*1024});
   assert.equal(r.stdout.includes(privateKey),false);return {exit_code:r.status,output:r.stdout,wall_time_seconds:0};},
  async mcp__codex_apps__google_drive_get_document({document_id}){counters.read++;return {structuredContent:structuredClone(docs.get(document_id)),isError:false};},
  async mcp__codex_apps__google_drive_batch_update_document(args){
   counters.write++;const doc=docs.get(args.document_id);assert.equal(args.write_control.requiredRevisionId,doc.revisionId);assert.equal(args.write_control.targetRevisionId,undefined);
   let text=docText(doc);for(const update of args.requests){
    if(update.deleteContentRange){const r=update.deleteContentRange.range;assert.equal(r.tabId,doc.tabs[0].tabId);assert.equal(r.startIndex,1);assert.equal(r.endIndex,text.length);text=text.slice(r.endIndex-1);}
    else {assert.ok(update.insertText);const r=update.insertText;assert.equal(r.location.tabId,doc.tabs[0].tabId);assert.equal(r.location.index,1);text=r.text+text;}
   }
   const record=JSON.parse(text),lose=options.loseAckPhase===record.phase;
   if(options.skipWritePhase!==record.phase)docs.set(args.document_id,resource(args.document_id,text,'revision-'+(++serial)));
   effects.push(record.phase);
   if(lose){options.loseAckPhase=null;throw Error('provider message with sensitive secret must never leave adapter');}
   if(options.skipWritePhase===record.phase)return {isError:true,content:[{type:'text',text:'sensitive provider message'}]};
   return {structuredContent:{documentId:args.document_id,document_url:'https://docs.google.com/document/d/'+args.document_id+'/edit',replies:args.requests.map(()=>({})),writeControl:{requiredRevisionId:docs.get(args.document_id).revisionId,targetRevisionId:null}},isError:false};
  },
  async mcp__codex_apps__google_drive_get_file_metadata({fileId}){counters.metadata++;const value=structuredClone(goldenMetadata);Object.assign(value.structuredContent,{id:fileId,size:String(blobs.get(fileId).length),url:'https://drive.google.com/file/d/'+fileId+'/view?usp=drivesdk'});return value;},
  async mcp__codex_apps__google_drive_fetch(args){counters.fetch++;assert.equal(args.download_raw_file,true);assert.equal(args.include_base64,false);const id=args.url.split('/')[5];
   const value=structuredClone(goldenRaw);for(const r of [value.structuredContent,value.structuredContent.structuredContent])Object.assign(r,{id,file_size_bytes:blobs.get(id).length,file_uri:{file_id:'sediment://file_'+id}});
   if(options.inlinePayload)value.structuredContent.content='premature plaintext';return value;},
  async download_file({file_id}){counters.download++;const id=file_id.slice(5),raw=blobs.get(id),dest=path.join(f.tmp,file_id+'.json');fs.writeFileSync(dest,raw,{mode:0o644});return {path:dest,size_bytes:raw.length};},
  async mcp__codex_apps__google_drive_upload_file(args){counters.upload++;assert.equal(args.parent_folder_id,'fixture-folder');const id='result-upload-'+serial;blobs.set(id,fs.readFileSync(args.file_uri));return {structuredContent:{id,title:args.file_name,success:true},isError:false};}
 };
 return {tools,docs,blobs,counters,effects};
}
function compactLoaderBytes(emitted,source){
 // Loader compactness is a bound on executable scaffolding, not the caller's
 // absolute paths. Parse only the four known JSON data literals emitted by
 // cli.py; keep all executable source, hash checks and source-cache keys in
 // the original 2400-byte budget. Unknown emission shapes fail this test.
 assert.equal(emitted.loader_bytes,Buffer.byteLength(source,'utf8'));
 const command=/tools\.exec_command\((\{[^\n]*\})\);if\(r\.exit_code/.exec(source);
 const config=/\)\)\(tools,(\{[^\n]*\}),store,load,/.exec(source);
 const run=/^const outcome=await adapter\.run\(("(?:\\.|[^"\\])*"),(\{[^\n]*\})\);outcome\.capture_key=/m.exec(source);
 assert.ok(command && config && run,'known file-backed loader data boundaries required');
 assert.ok(source.startsWith('// @exec: {"yield_time_ms": 1000, "max_output_tokens": 32768}'));// Outer budget matters too.
 const literals=[command[1],config[1],run[1],run[2]],parsed=literals.map(x=>JSON.parse(x));
 assert.equal(parsed[0].cmd,'python3 -B -m dots_lite.cli load-cell --sha256 '+emitted.source_sha256);
 assert.equal(parsed[0].workdir,parsed[1].cwd);
 assert.equal(typeof parsed[2],'string');assert.equal(typeof parsed[3],'object');assert.ok(parsed[3] && !Array.isArray(parsed[3]));
 assert.ok(!source.includes('function createLiteNativeAdapter('),'adapter must remain file-backed');
 const data_bytes=literals.reduce((n,x)=>n+Buffer.byteLength(x,'utf8'),0),fixed_bytes=emitted.loader_bytes-data_bytes;
 assert.ok(fixed_bytes<2400,'loader fixed overhead '+fixed_bytes+' must remain below 2400 bytes');
 return {loader_bytes:emitted.loader_bytes,data_bytes,fixed_bytes};
}
function makeExecutor(f,provider,options={}){let n=0;const store=options.memory || new Map(),measurements=[];const execute=async function(action,{stateDir=f.root,actor='/root',route='alpha',args={}}={}){
 const nonce=require('node:crypto').randomBytes(8).toString('hex');const file=path.join(f.tmp,'cell-'+nonce+'-'+(++n)+'.js'),argsFile=privateJSON(path.join(f.tmp,'arguments-'+nonce+'-'+n+'.json'),args);
 const emitted=command(f.cwd,['-m','dots_lite.cli','emit-cell',action,'--state-dir',stateDir,'--actor-task-id',actor,'--route-id',route,'--arguments-file',argsFile,'--save',file]);
 assert.equal(emitted.ok,true,JSON.stringify(emitted));const source=fs.readFileSync(file,'utf8');measurements.push({action,...compactLoaderBytes(emitted,source)});execute.lastLoader={emitted,source};let output;const emittedBlocks=[];
 await new AsyncFunction('tools','load','store','text',source)(provider.tools,k=>store.get(k),(k,v)=>{if(options.failCapture && k.startsWith('dots-lite-captures-'))throw Error('private sink error');store.set(k,v);},v=>{emittedBlocks.push(v);output=v;});if(output.acquisition_chunk_count!==undefined){const blocks=structuredClone(emittedBlocks);output.text=emittedBlocks.filter(x=>typeof x==='string').join('');output.emitted_blocks=blocks;}return output;
};execute.memory=store;execute.loaderMeasurements=measurements;return execute;}
async function admit(f,exec,route='alpha'){
 const reserved=await exec('parent-prepare',{route});assert.equal(reserved.ok,true,JSON.stringify(reserved));
 const args=reserved.spawn_arguments;assert.equal(args.fork_turns,'none');assert.equal(args.model,'gpt-6.1-sol');assert.equal(args.reasoning_effort,'xhigh');
 // This is a synthetic platform fixture, not a live native-spawn assertion.
 const result={task_name:'/root/'+args.task_name,agent_id:'synthetic-'+route};
 const submitted=privateJSON(path.join(f.tmp,'spawn-args-'+route+'.json'),args),actual=privateJSON(path.join(f.tmp,'spawn-result-'+route+'.json'),result);
 const admitted=await exec('parent-admit',{route,args:{actualArgumentsFile:submitted,nativeResultFile:actual}});assert.equal(admitted.ok,true,JSON.stringify(admitted));
 const child={stateDir:admitted.child_state_dir,actor:result.task_name,route};
 assert.equal((await exec('child-takeover',{...child,args:{handoff_file:admitted.handoff_file}})).ok,true);
 return child;
}

function acquisitionArgs(begin){return {requestId:begin.request_id,requestSha256:begin.request_sha256,packageSha256:begin.binding.package_sha256};}
async function acquireChunks(exec,child,begin,action,extra={}){
 const chunks=[];let offset=0,digest=null,total=null;
 do {const r=await exec(action,{...child,args:{...acquisitionArgs(begin),...extra,offset,maxBytes:4096}});
  assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.offset,offset);assert.equal(Buffer.byteLength(r.text),r.next_offset-r.offset);
  if(digest!==null){assert.equal(r.sha256,digest);assert.equal(r.total_bytes,total);}digest=r.sha256;total=r.total_bytes;
  chunks.push(r.text);offset=r.next_offset;if(r.complete)break;
 }while(true);
 const raw=Buffer.from(chunks.join(''));assert.equal(raw.length,total);assert.equal(require('node:crypto').createHash('sha256').update(raw).digest('hex'),digest);
 return JSON.parse(raw);
}
async function beginAndAcquire(exec,child){const begin=await exec('child-begin',child);if(begin.ok && begin.status==='exposed')begin.view=await acquireChunks(exec,child,begin,'child-view-read');return begin;}
async function acquireSchema(exec,child,begin,namespace,name){return acquireChunks(exec,child,begin,'child-tool-schema',{namespace,name});}
async function retryArguments(exec,child,priorDisposition='provider_unknown'){
 const state=await exec('child-upload-retry-status',child);assert.equal(state.ok,true,JSON.stringify(state));
 return {expectedResultId:state.result_id,expectedResultSha256:state.result_sha256,expectedAttempt:state.next_attempt,expectedFolderId:state.folder_id,
  review:{decision:'same_immutable_result_upload_after_raw_and_permission_review',controller_task_id:child.actor,
   permission_reference:'synthetic-fresh-user-consent',raw_result_reference:'synthetic-actual-upload-result',prior_disposition:priorDisposition}};
}

function resultRetryArgs(state,child,priorDisposition='provider_unknown'){
 return {expectedOperationId:state.operation_id,expectedAttempt:state.next_attempt,expectedPlanSha256:state.plan_sha256,
  expectedDocumentId:state.document_id,expectedRequiredRevisionId:state.required_revision_id,
  review:{decision:'same_result_cas_after_raw_and_permission_review',controller_task_id:child.actor,
   permission_reference:'synthetic-fresh-result-consent',raw_result_reference:'synthetic-actual-result-write',prior_disposition:priorDisposition}};
}
function output(f,id='response-one'){return privateJSON(path.join(f.tmp,id+'.json'),{id,object:'response',status:'completed',model:'gpt-6.1-sol',output:[{id:'message-one',type:'message',role:'assistant',content:[{type:'output_text',text:'Synthetic result 😀'}]}]});}

test('generated loader + real Python helpers: 2 routes, normalized provider, five warm helpers, no readbacks',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p);const a=await admit(f,exec),b=await admit(f,exec,'beta');
  const before={...p.counters};const begin=await beginAndAcquire(exec,a);assert.equal(begin.ok,true,JSON.stringify(begin));assert.equal(begin.status,'exposed');assert.equal(begin.view.request.input[0].content,'Unicode 😀 private payload alpha');
  const done=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});assert.equal(done.ok,true,JSON.stringify(done));
  const count=Object.fromEntries(Object.keys(before).map(k=>[k,p.counters[k]-before[k]]));
  assert.deepEqual(count,{exec:6,read:1,write:2,metadata:1,fetch:1,download:1,upload:1});
  assert.equal(JSON.parse(docText(p.docs.get('outbox-alpha'))).phase,'RESULT');assert.equal(JSON.parse(docText(p.docs.get('outbox-beta'))).phase,'ADMITTED');
  assert.deepEqual(p.effects,['SPAWN_RESERVED','ADMITTED','SPAWN_RESERVED','ADMITTED','BEGIN','RESULT']);
  const replay=await beginAndAcquire(exec,a);assert.equal(replay.ok,false);assert.equal(p.counters.upload,1);
  assert.equal((await beginAndAcquire(exec,b)).ok,true);
 }finally{f.cleanup();}
});
test('lost BEGIN and RESULT acknowledgements reconcile same operations without replay or extra upload',async()=>{
 const f=fixture();try{const options={loseAckPhase:'BEGIN'},p=fakeTools(f,options),exec=makeExecutor(f,p),a=await admit(f,exec);
  const begin=await beginAndAcquire(exec,a);assert.equal(begin.ok,true,JSON.stringify(begin));assert.equal(begin.status,'exposed');
  options.loseAckPhase='RESULT';
  assert.equal((await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}})).ok,true);
  assert.equal(p.counters.upload,1);assert.deepEqual(p.effects,['SPAWN_RESERVED','ADMITTED','BEGIN','RESULT']);
 }finally{f.cleanup();}
});
test('unobserved write freezes only that route; crash after exposure never reissues input',async()=>{
 const f=fixture();try{const options={skipWritePhase:'BEGIN'},p=fakeTools(f,options),exec=makeExecutor(f,p),a=await admit(f,exec),b=await admit(f,exec,'beta');
  const unknown=await beginAndAcquire(exec,a);assert.equal(unknown.status,'unknown');
  assert.equal((await beginAndAcquire(exec,a)).ok,false);options.skipWritePhase=null;
  const begin=await beginAndAcquire(exec,b);assert.equal(begin.status,'exposed');
  const restarted=makeExecutor(f,p);const again=await restarted('child-begin',b);assert.equal(again.ok,false);
  const journal=JSON.parse(fs.readFileSync(path.join(b.stateDir,'journal.json'))).state;assert.equal(journal.current.exposure,'EXPOSED');
  assert.equal(p.effects.filter(x=>x==='BEGIN').length,2);assert.equal(p.counters.upload,0);
 }finally{f.cleanup();}
});
test('inline raw text is refused before prepare/exposure; no secrets in returned error',async()=>{
 const f=fixture();try{const p=fakeTools(f,{inlinePayload:true}),exec=makeExecutor(f,p),a=await admit(f,exec),r=await beginAndAcquire(exec,a);
  assert.equal(r.ok,false);assert.equal(r.error.code,'lite_raw_reference_invalid');assert.equal(p.counters.download,0);assert.ok(!JSON.stringify(r).includes('premature'));
  assert.equal(JSON.parse(fs.readFileSync(path.join(a.stateDir,'journal.json'))).state.phase,'ADMITTED');
 }finally{f.cleanup();}
});
module.exports={fixture,fakeTools,makeExecutor,admit,resource,docText,privateJSON,output};

test('spawn fence remains burned when actual platform result is missing; other route still admits',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p);
  const reserved=await exec('parent-prepare');assert.equal(reserved.ok,true);
  const repeated=await exec('parent-prepare');assert.equal(repeated.ok,false);
  assert.equal(p.effects.filter(x=>x==='SPAWN_RESERVED').length,1);
  assert.equal((await admit(f,exec,'beta')).route,'beta');
  const state=JSON.parse(fs.readFileSync(path.join(f.root,'route-alpha','journal.json'))).state;
  assert.equal(state.spawn_fence,'ISSUED');assert.equal(state.actual_tool_result,null);
 }finally{f.cleanup();}
});
test('actual submitted arguments cannot be changed or fabricated from desired admission',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),reserved=await exec('parent-prepare');
  const changed={...reserved.spawn_arguments,reasoning_effort:'high'};
  const r=await exec('parent-admit',{args:{actualArgumentsFile:privateJSON(path.join(f.tmp,'changed.json'),changed),nativeResultFile:privateJSON(path.join(f.tmp,'actual.json'),{task_name:'/root/'+changed.task_name})}});
  assert.equal(r.ok,false);assert.equal(r.error.code,'actual_spawn_arguments_mismatch');assert.deepEqual(p.effects,['SPAWN_RESERVED']);
 }finally{f.cleanup();}
});
test('interrupted local handoff export repairs the same actual child without spawn or Docs writes',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),writes=p.counters.write;
  fs.unlinkSync(path.join(a.stateDir,'native-config.json'));fs.unlinkSync(path.join(a.stateDir,'native-handoff.json'));
  const recovered=await exec('parent-recover-handoff');assert.equal(recovered.ok,true,JSON.stringify(recovered));
  assert.equal(recovered.child_state_dir,a.stateDir);assert.equal(recovered.handoff_arguments.target,a.actor);assert.equal(p.counters.write,writes);
  assert.equal((await exec('child-takeover',a)).ok,true);
 }finally{f.cleanup();}
});
test('three immutable upload attempts maximum; lost upload replies do not re-infer or invent results',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a);
  const saved=[];p.tools.mcp__codex_apps__google_drive_upload_file=async args=>{p.counters.upload++;saved.push(fs.readFileSync(args.file_uri));throw Error('lost response with private data');};
  const first=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});assert.equal(first.status,'upload_unknown');
  assert.equal((await exec('child-retry-upload',a)).error.code,'upload_result_binding_mismatch');
  const reviewed={...a,args:await retryArguments(exec,a)};
  assert.equal((await exec('child-retry-upload',reviewed)).status,'upload_unknown');
  assert.equal((await exec('child-retry-upload',{...a,args:await retryArguments(exec,a)})).status,'upload_unknown');
  assert.equal((await exec('child-retry-upload',reviewed)).ok,false);assert.equal(p.counters.upload,3);assert.ok(saved.every(x=>x.equals(saved[0])));
  assert.equal((await beginAndAcquire(exec,a)).ok,false);
 }finally{f.cleanup();}
});

test('same admitted child receives full function/custom continuation with original call IDs',async()=>{
 const client=JSON.parse(fs.readFileSync(path.join(__dirname,'fixtures/codex-0.159.2-tool-items.json'))),requestTools=client.tools;
 const f=fixture(requestTools);try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a);
  assert.equal(begin.status,'exposed');const originalRequest=JSON.parse(f.files['request-alpha']);assert.deepEqual(begin.view.request.input,originalRequest.input);
  const items=structuredClone(client.items);assert.ok(items.every(x=>/^[^_]+_.+$/.test(x.id)));assert.equal(/^[^_]+_.+$/.test(client.incompatible_id),false);
  await acquireSchema(exec,a,begin,'functions','exec_command');await acquireSchema(exec,a,begin,'functions','apply_patch');
  const response={id:'response-tools',object:'response',status:'completed',model:'gpt-6.1-sol',output:items};
  const first=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:privateJSON(path.join(f.tmp,'actual-tool-output.json'),response)}});assert.equal(first.ok,true,JSON.stringify(first));
  const outbox=JSON.parse(docText(p.docs.get('outbox-alpha')));const input=[...originalRequest.input,...items,...client.outputs];
  const body={...originalRequest,input},bodyFile=privateJSON(path.join(f.tmp,'second-request.json'),body);
  const inboxFile=privateJSON(path.join(f.tmp,'inbox-current.json'),JSON.parse(docText(p.docs.get('inbox-test'))));
  const update=String.raw`
import json,sys
from pathlib import Path
from dots_lite.protocol import *
from dots_lite.private_io import read
config=read(Path(sys.argv[1])/'native-config.json');inbox=read(sys.argv[2]);raw=Path(sys.argv[3]).read_bytes();outbox=read(sys.argv[4]);route=next(r for r in inbox['routes'] if r['route_id']=='alpha');d=route['request']
route['request']={**d,'seq':2,'request_id':'request-alpha-2','request_sha256':sha256(raw),'byte_length':len(raw),'file_id':'request-alpha-2','previous_result_ack':{k:outbox['result'][k] for k in ('result_id','result_sha256')}}
inbox['operation_id']='second-inbox-operation';print(json.dumps(sign_record(inbox,config['join']['join_code'])))
`;
  const secondInbox=command(f.cwd,['-c',update,f.root,inboxFile,bodyFile,privateJSON(path.join(f.tmp,'first-outbox.json'),outbox)]);
  p.docs.set('inbox-test',resource('inbox-test',canonical(secondInbox)+'\n','inbox-next'));p.blobs.set('request-alpha-2',fs.readFileSync(bodyFile));
  const continued=await beginAndAcquire(exec,a);assert.equal(continued.ok,true,JSON.stringify(continued));assert.equal(continued.request_id,'request-alpha-2');
  assert.deepEqual(continued.view.request.input,input);
  assert.equal((await exec('child-complete',{...a,args:{requestId:continued.request_id,outputFile:output(f,'response-two')}})).ok,true);
  assert.equal(p.effects.filter(x=>x==='SPAWN_RESERVED').length,1);assert.equal(p.counters.upload,2);
 }finally{f.cleanup();}
});

test('activation capacity includes an unresolved real-spawn reservation',async()=>{
 const f=fixture();try{const file=path.join(f.root,'native-config.json'),config=JSON.parse(fs.readFileSync(file));config.available_child_slots=1;privateJSON(file,config);
  const p=fakeTools(f),exec=makeExecutor(f,p);assert.equal((await exec('parent-prepare')).ok,true);
  const blocked=await exec('parent-prepare',{route:'beta'});assert.equal(blocked.ok,false);assert.equal(blocked.error.code,'native_capacity_reached');
  assert.deepEqual(p.effects,['SPAWN_RESERVED']);assert.equal(Object.keys(JSON.parse(fs.readFileSync(file)).routes).length,1);
 }finally{f.cleanup();}
});

const captureConfig={cwd:'/fixture',stateDir:'/private',actorTaskId:'/root/child',inboxId:'inbox'};
test('capture primitive preserves exact arguments, return and throw identities, with sink only after dispatch',async()=>{
 const events=[],args={file_uri:'/private/result.json'},returned={isError:true,content:[{type:'text',text:'entire private denial'}],structuredContent:{code:'approval_denied'}},thrown={message:'exact thrown object',nested:{secret:'private'}},seen=[];
 const adapter=createLiteNativeAdapter({upload:async submitted=>{events.push('dispatch');assert.equal(submitted,args);return returned;},fail:async submitted=>{events.push('throw');assert.equal(submitted,args);throw thrown;}},{...captureConfig,captureSink:r=>{events.push('sink');seen.push(r);return true;}});
 assert.equal(await adapter.callCaptured('upload',args),returned);
 await assert.rejects(adapter.callCaptured('fail',args),e=>e===thrown);
 assert.deepEqual(events,['dispatch','sink','throw','sink']);assert.equal(seen[0].args,args);assert.equal(seen[0].value,returned);assert.equal(seen[1].error,thrown);
 assert.equal(adapter.getCaptures()[0],seen[0]);assert.equal(returned.isError,true);
 for(let i=0;i<34;i++)await adapter.callCaptured('upload',args);
 assert.equal(adapter.getCaptures().length,32);
});

test('JSON-backed session capture retains nested envelopes and exception data without invoking accessors',()=>{
 const memory=new Map(),sink=createLiteMemoryCaptureSink((k,v)=>memory.set(k,JSON.parse(JSON.stringify(v))),k=>memory.get(k),'memory-key');
 const full={isError:true,content:[{type:'text',text:'private approval reason'}],structuredContent:{isError:true,error:{code:'approval_denied'},provider_trace:{nested:['unaltered']}}};
 sink({tool:'upload',args:{file_uri:'/private'},kind:'return',value:full});
 const second=createLiteMemoryCaptureSink((k,v)=>memory.set(k,JSON.parse(JSON.stringify(v))),k=>memory.get(k),'memory-key');
 const error=Error('entire thrown private reason',{cause:{nested:'private cause'}});error.code='ETIMEDOUT';
 second({tool:'upload',args:{file_uri:'/private'},kind:'throw',error});
 assert.deepEqual(memory.get('memory-key')[0].value,full);
 assert.equal(memory.get('memory-key')[1].error.message,error.message);assert.deepEqual(memory.get('memory-key')[1].error.cause,error.cause);assert.equal(memory.get('memory-key')[1].error.code,'ETIMEDOUT');
 let getterCalls=0;const dangerous={};Object.defineProperty(dangerous,'secret',{get(){getterCalls++;return 'should never read';},enumerable:true});
 assert.throws(()=>sink({kind:'return',value:dangerous}),/lite_capture_failed/);assert.equal(getterCalls,0);
 assert.equal(memory.get('memory-key').length,2);
});

test('generated loaders retain full normalized approval denial across cells and forbid upload retry',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a);
  const full={isError:true,content:[{type:'text',text:'private approval rejection with all explanatory details'}],structuredContent:{error:{code:'approval_denied',message:'private policy reason'},status:403,extra:{entire:'unchanged'}}};
  const originalExec=p.tools.exec_command;p.tools.exec_command=async args=>{assert.ok(!args.cmd.includes('private approval rejection'));assert.ok(!args.cmd.includes('private policy reason'));const b64=/'--input-base64' '([^']*)'/.exec(args.cmd);if(b64)assert.ok(!Buffer.from(b64[1],'base64').toString().includes('private policy reason'));return originalExec(args);};
  p.tools.mcp__codex_apps__google_drive_upload_file=async()=>{p.counters.upload++;return full;};
  const done=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});
  assert.equal(done.status,'upload_blocked');assert.equal(done.error.category,'approval_blocked');assert.equal(done.error.code,'lite_approval_blocked');assert.equal(done.error.status,403);
  assert.ok(!JSON.stringify(done).includes('private policy reason'));
  const capture=exec.memory.get(done.capture_key).find(x=>x.stage==='result_upload');assert.deepEqual(capture.value,full);assert.equal(capture.value.isError,true);
  const next=makeExecutor(f,p,{memory:exec.memory});assert.equal((await next('status',a)).ok,true);assert.deepEqual(next.memory.get(done.capture_key).find(x=>x.stage==='result_upload').value,full);
  const retry=await next('child-retry-upload',{...a,args:{retryDecision:'transport_retry_after_raw_review'}});assert.equal(retry.error.code,'upload_result_binding_mismatch');assert.equal(p.counters.upload,1);
  assert.equal((await next('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}})).error.code,'upload_retry_requires_explicit_review');assert.equal(p.counters.upload,1);
  assert.equal(p.effects.filter(x=>x==='RESULT').length,0);
 }finally{f.cleanup();}
});

test('unknown denial strings stay unknown and pause for raw review without automatic retry or inference',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a);
  const full={isError:true,content:[{type:'text',text:'The reviewer denied this action. 403. Do not retry.'}],structuredContent:null};
  p.tools.mcp__codex_apps__google_drive_upload_file=async()=>{p.counters.upload++;return full;};
  const done=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});
  assert.equal(done.status,'upload_unknown');assert.equal(done.error.category,'provider_unknown');assert.equal(done.error.status,undefined);
  assert.deepEqual(exec.memory.get(done.capture_key).find(x=>x.stage==='result_upload').value,full);
  assert.equal((await exec('child-retry-upload',a)).error.code,'upload_result_binding_mismatch');assert.equal(p.counters.upload,1);assert.equal(p.effects.filter(x=>x==='BEGIN').length,1);
 }finally{f.cleanup();}
});

test('capture store failure pauses after actual upload and before any dependent helper or write',async()=>{
 const f=fixture();try{const p=fakeTools(f),options={},exec=makeExecutor(f,p,options),a=await admit(f,exec),begin=await beginAndAcquire(exec,a),before={...p.counters};
  options.failCapture=true;
  const done=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});
  assert.equal(done.error.code,'lite_capture_failed');assert.equal(p.counters.upload-before.upload,1);assert.equal(p.counters.exec-before.exec,1);assert.equal(p.counters.write-before.write,0);
  options.failCapture=false;
  assert.equal((await exec('child-retry-upload',{...a,args:await retryArguments(exec,a)})).error.code,'upload_failure_capture_review_required');assert.equal(p.counters.upload-before.upload,1);
 }finally{f.cleanup();}
});

test('safe diagnostics count actual stages and label clocks without claiming inference or model-read time',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a),done=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});
  assert.equal(begin.diagnostics.helper_calls+done.diagnostics.helper_calls,5);assert.equal(begin.diagnostics.provider_calls+done.diagnostics.provider_calls,7);
  assert.equal(done.diagnostics.request_file_read_ms,null);assert.equal(done.diagnostics.native_inference_ms,null);
  const stages=[...begin.diagnostics.stages,...done.diagnostics.stages];
  assert.ok(stages.some(x=>x.stage==='input_download'));assert.ok(stages.some(x=>x.stage==='result_upload'));
  assert.ok(stages.every(x=>x.duration_ms>=0 && ['performance.now','Date.now'].includes(x.clock_source)));
  assert.ok(stages.some(x=>(x.local?.stages || []).some(x=>x.stage==='native_response_file_read' && x.clock_source==='time.perf_counter')));
  assert.equal(done.loader_diagnostics.cold_source_load,false);assert.equal(done.loader_diagnostics.source_helper_calls,0);
 }finally{f.cleanup();}
});

test('explicit RESULT-only retry republishes exact sealed CAS after raw/permission review without upload or inference',async()=>{
 const f=fixture();try{const options={},p=fakeTools(f,options),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a);
  options.skipWritePhase='RESULT';const written=[],original=p.tools.mcp__codex_apps__google_drive_batch_update_document;
  p.tools.mcp__codex_apps__google_drive_batch_update_document=async args=>{written.push(structuredClone(args));return original(args);};
  const first=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});assert.equal(first.status,'unknown');
  const state=await exec('child-result-retry-status',a);assert.equal(state.ok,true,JSON.stringify(state));assert.equal(state.attempts_used,1);assert.equal(state.next_attempt,2);
  assert.equal((await exec('child-retry-result',a)).ok,false);assert.equal(written.length,1);
  options.skipWritePhase=null;const before={...p.counters};
  const done=await exec('child-retry-result',{...a,args:resultRetryArgs(state,a)});
  assert.equal(done.ok,true,JSON.stringify(done));assert.deepEqual(written[1],written[0]);assert.equal(p.counters.upload,before.upload);assert.equal(p.counters.write,before.write+1);assert.equal(p.effects.filter(x=>x==='BEGIN').length,1);
  assert.equal(JSON.parse(docText(p.docs.get('outbox-alpha'))).phase,'RESULT');
  assert.equal((await exec('child-retry-result',{...a,args:resultRetryArgs(state,a)})).ok,false);assert.equal(written.length,2);
 }finally{f.cleanup();}
});


test('loader fixed-overhead budget is path-independent and still rejects executable growth',async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'dots-lite-long-loader-'));
 const nested=path.join(root,...Array.from({length:4},(_,i)=>'isolated-validation-path-'+i+'-'+('x'.repeat(48))),`quoted-漢-😀-"-'`);
 fs.mkdirSync(nested,{recursive:true,mode:0o700});const f=fixture([],{temporaryRoot:nested});
 try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a);
  assert.equal(begin.status,'exposed');assert.equal((await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}})).ok,true);
  assert.ok(exec.loaderMeasurements.some(x=>x.action==='parent-admit' && x.loader_bytes>2400));
  assert.equal(new Set(exec.loaderMeasurements.map(x=>x.fixed_bytes)).size,1);
  assert.ok(exec.loaderMeasurements.every(x=>x.loader_bytes===x.data_bytes+x.fixed_bytes));
  const {emitted,source}=exec.lastLoader,grown=source+'\n'+('void 0;\n'.repeat(400));
  assert.throws(()=>compactLoaderBytes({...emitted,loader_bytes:Buffer.byteLength(grown,'utf8')},grown),/fixed overhead/);
  assert.throws(()=>compactLoaderBytes({...emitted,loader_bytes:emitted.loader_bytes+1},source),assert.AssertionError);
 }finally{f.cleanup();fs.rmSync(root,{recursive:true,force:true});}
});

test('fresh transport scope is required before any local activation allocation',()=>{
 const f=fixture();try{const raw=fs.readFileSync(path.join(f.tmp,'join.txt'),'utf8'),join=JSON.parse(raw.slice(raw.indexOf(' ')+1));delete join.transport_authorization;
  const legacy=path.join(f.tmp,'legacy-join.txt');fs.writeFileSync(legacy,'DOTS2CODEX_GLOBAL_JOIN_V3 '+JSON.stringify(join),{mode:0o600});const state=path.join(f.tmp,'must-not-exist');
  const r=command(f.cwd,['-m','dots_lite.cli','init-parent','--state-dir',state,'--join-file',legacy,'--actor-task-id','/root','--authorization-message-id','synthetic-message','--available-child-slots','1']);
  assert.equal(r.ok,false);assert.equal(r.error.code,'fresh_transport_authorization_required');assert.equal(fs.existsSync(state),false);
 }finally{f.cleanup();}
});

test('model and exact schema acquisition stay bounded, hash-bound and fenced across session loss',async()=>{
 const tools=[{type:'namespace',name:'functions',description:'Preserve complete namespace instructions',tools:[{type:'function',name:'exec_command',description:'Long schema text '.repeat(1000),parameters:{type:'object',properties:{cmd:{type:'string'}},required:['cmd'],additionalProperties:false}}]}];
 const f=fixture(tools);try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec);
  const early=await exec('child-view-read',{...a,args:{requestId:'request-id-alpha'}});assert.equal(early.error.code,'lite_exposure_continuation_required');
  const begin=await exec('child-begin',a);assert.equal(begin.status,'exposed');assert.equal(begin.exposure_token,undefined);assert.equal(begin.exposed_path,undefined);
  const held=exec.memory.get(begin.capture_key+'-exposure');assert.equal(typeof held.token,'string');assert.ok(!JSON.stringify(begin).includes(held.token));
  const pending=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});assert.equal(pending.error.code,'request_view_incomplete');assert.equal(p.counters.upload,0);
  const wrong=await exec('child-view-read',{...a,args:{...acquisitionArgs(begin),requestSha256:'0'.repeat(64)}});assert.equal(wrong.error.code,'lite_exposure_binding_mismatch');
  const view=await acquireChunks(exec,a,begin,'child-view-read');assert.deepEqual(view.request.input,JSON.parse(f.files['request-alpha']).input);assert.equal(view.request.tools[0].description,tools[0].description);assert.ok(!JSON.stringify(view).includes(tools[0].tools[0].description));
  const call={id:'response_schema_gate',object:'response',status:'completed',model:'gpt-6.1-sol',output:[{type:'function_call',id:'fc_12345',namespace:'functions',name:'exec_command',call_id:'call_unchanged',arguments:'{"cmd":"pwd"}'}]},out=privateJSON(path.join(f.tmp,'schema-call.json'),call);
  const missing=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:out}});assert.equal(missing.ok,false);assert.equal(p.counters.upload,0);
  const partial=await exec('child-tool-schema',{...a,args:{...acquisitionArgs(begin),namespace:'functions',name:'exec_command',maxBytes:32}});assert.equal(partial.ok,true);assert.equal(partial.complete,false);
  assert.equal((await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:out}})).ok,false);
  // Same-session reread after a truncated display only repeats already-issued bytes.
  const journalBeforeReread=fs.readFileSync(path.join(a.stateDir,'journal.json'));
  const reread=await exec('child-tool-schema',{...a,args:{...acquisitionArgs(begin),namespace:'functions',name:'exec_command',maxBytes:32}});assert.equal(reread.text,partial.text);assert.equal(reread.next_offset,partial.next_offset);assert.ok(fs.readFileSync(path.join(a.stateDir,'journal.json')).equals(journalBeforeReread));
  let offset=partial.next_offset;do{const chunk=await exec('child-tool-schema',{...a,args:{...acquisitionArgs(begin),namespace:'functions',name:'exec_command',offset,maxBytes:4096}});assert.equal(chunk.ok,true,JSON.stringify(chunk));assert.equal(chunk.offset,offset);offset=chunk.next_offset;if(chunk.complete)break;}while(true);
  const restarted=makeExecutor(f,p);const lost=await restarted('child-complete',{...a,args:{requestId:begin.request_id,outputFile:out}});assert.equal(lost.error.code,'lite_exposure_continuation_required');assert.equal(p.counters.upload,0);
  assert.equal((await restarted('child-begin',a)).ok,false);assert.equal(p.effects.filter(x=>x==='BEGIN').length,1);
  const done=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:out}});assert.equal(done.ok,true,JSON.stringify(done));assert.equal(p.counters.upload,1);
 }finally{f.cleanup();}
});

test('fresh reviewed upload retry preserves the original permission denial and stops on a second denial',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a),raw={isError:true,structuredContent:{error:{code:'approval_denied',message:'private exact denial'}}};
  const attempts=[];p.tools.mcp__codex_apps__google_drive_upload_file=async args=>{p.counters.upload++;attempts.push(fs.readFileSync(args.file_uri));return raw;};
  assert.equal((await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}})).status,'upload_blocked');
  const args=await retryArguments(exec,a,'permission_denied');const retried=await exec('child-retry-upload',{...a,args});assert.equal(retried.status,'upload_blocked');assert.equal(retried.retry_blocked,true);assert.equal(attempts.length,2);assert.ok(attempts[0].equals(attempts[1]));
  const status=await exec('child-upload-retry-status',a);assert.equal(status.original_failure.category,'approval_blocked');assert.equal(status.latest_failure.category,'approval_blocked');assert.equal(status.reviews[0].prior_disposition,'permission_denied');assert.equal(status.next_attempt,null);
  assert.equal(exec.memory.get(retried.capture_key).filter(x=>x.stage==='result_upload').length,2);assert.deepEqual(exec.memory.get(retried.capture_key).filter(x=>x.stage==='result_upload')[0].value,raw);
  assert.equal((await exec('child-retry-upload',{...a,args:{...args,expectedAttempt:3}})).ok,false);assert.equal(p.counters.upload,2);assert.equal(p.effects.filter(x=>x==='RESULT').length,0);
 }finally{f.cleanup();}
});

test('observed ACK document_url is inert and unknown structure still requires exact read reconciliation',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec);const before=p.counters.read,begin=await beginAndAcquire(exec,a);assert.equal(p.counters.read,before+1);
  const raw=exec.memory.get(begin.capture_key).find(x=>x.stage==='child-expose').value;assert.equal(typeof raw.structuredContent.document_url,'string');
  const write=p.tools.mcp__codex_apps__google_drive_batch_update_document;p.tools.mcp__codex_apps__google_drive_batch_update_document=async args=>{const r=await write(args);r.structuredContent.unrecognizedTopology={documentId:'untrusted',replies:[]};return r;};
  const reads=p.counters.read;assert.equal((await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}})).ok,true);assert.equal(p.counters.read,reads+1);
 }finally{f.cleanup();}
});

test('specific fresh consent can complete the same denied upload and exact denied RESULT plan',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a);
  const upload=p.tools.mcp__codex_apps__google_drive_upload_file,write=p.tools.mcp__codex_apps__google_drive_batch_update_document;
  const denial={isError:true,structuredContent:{error:{code:'permission_denied',message:'private actual permission decision'}},content:[{type:'text',text:'full private explanation'}]};let deniedUpload=true,deniedWrite=true;const uploadBytes=[],writeArgs=[];
  p.tools.mcp__codex_apps__google_drive_upload_file=async args=>{uploadBytes.push(fs.readFileSync(args.file_uri));if(deniedUpload){p.counters.upload++;return denial;}return upload(args);};
  p.tools.mcp__codex_apps__google_drive_batch_update_document=async args=>{writeArgs.push(structuredClone(args));if(deniedWrite){p.counters.write++;return denial;}return write(args);};
  assert.equal((await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}})).status,'upload_blocked');
  const reviewedUpload=await retryArguments(exec,a,'permission_denied');deniedUpload=false;
  const published=await exec('child-retry-upload',{...a,args:reviewedUpload});assert.equal(published.status,'unknown');assert.equal(uploadBytes.length,2);assert.ok(uploadBytes[0].equals(uploadBytes[1]));
  const pending=await exec('child-result-retry-status',a);assert.equal(pending.original_failure.category,'approval_blocked');assert.equal(pending.document_id,'outbox-alpha');assert.equal(pending.attempts_used,1);assert.equal(pending.next_attempt,2);
  const priorCounts={...p.counters};deniedWrite=false;const done=await exec('child-retry-result',{...a,args:resultRetryArgs(pending,a,'permission_denied')});assert.equal(done.ok,true,JSON.stringify(done));assert.deepEqual(writeArgs[1],writeArgs[0]);assert.equal(p.counters.upload,priorCounts.upload);assert.equal(p.counters.write,priorCounts.write+1);assert.equal(p.effects.filter(x=>x==='BEGIN').length,1);
  const state=JSON.parse(fs.readFileSync(path.join(a.stateDir,'journal.json'))).state;assert.equal(state.phase,'RESULT_COMMITTED');
 }finally{f.cleanup();}
});

test('saved immutable output before initial dispatch can upload once after reviewed fresh-session recovery',async()=>{
 const f=fixture();try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await beginAndAcquire(exec,a),base=p.tools.exec_command;let interrupt=true;
  p.tools.exec_command=async args=>{
   if(interrupt && args.cmd.includes("'--operation' 'child-save'")){interrupt=false;const encoded=/'--input-base64' '([^']*)'/.exec(args.cmd)[1];
    // Synthetic fault boundary: use the real save operation, then lose the
    // helper before its initial upload-reservation call. No source is changed.
    command(f.cwd,['-c',String.raw`
import base64,json,sys
from pathlib import Path
from dots_lite.cli import config_at,worker
from dots_lite.private_io import read
from dots_lite.protocol import strict_json
root=Path(sys.argv[1]);config=config_at(root);w=worker(root,config,sys.argv[2]);data=strict_json(base64.b64decode(sys.argv[3]))
w.save_actual_result(data['request_id'],read(data['output_file']),exposure_token=data['exposure_token'])
print(json.dumps({'ok':True}))
`,a.stateDir,a.actor,encoded]);return {exit_code:1,output:''};
   }return base(args);
  };
  const lost=await exec('child-complete',{...a,args:{requestId:begin.request_id,outputFile:output(f)}});assert.equal(lost.error.code,'lite_helper_failed');assert.equal(p.counters.upload,0);
  const state=JSON.parse(fs.readFileSync(path.join(a.stateDir,'journal.json'))).state;assert.equal(state.phase,'RESULT_SAVED');assert.equal(state.current.upload_attempts,0);const originalBytes=fs.readFileSync(state.current.artifact.path);
  const restarted=makeExecutor(f,p),review=await retryArguments(restarted,a,'not_attempted');assert.equal(review.expectedAttempt,1);review.review.raw_result_reference='synthetic-durable-save-observation';
  const done=await restarted('child-retry-upload',{...a,args:review});assert.equal(done.ok,true,JSON.stringify(done));assert.equal(p.counters.upload,1);assert.equal(p.effects.filter(x=>x==='BEGIN').length,1);
  const saved=JSON.parse(fs.readFileSync(path.join(a.stateDir,'journal.json'))).state;assert.equal(saved.current.upload_recovery.failures.length,0);assert.equal(saved.current.upload_recovery.reviews[0].prior_disposition,'not_attempted');assert.ok(p.blobs.get(saved.current.upload_receipt.file_id).equals(originalBytes));
 }finally{f.cleanup();}
});

test('bounded four-chunk cells preserve worst-escaped Unicode and EOF with readonly batch rereads',async()=>{
 const namespaceText=('\\"\u0001😀').repeat(1800),tools=[{type:'namespace',name:'functions',description:namespaceText,tools:[{type:'function',name:'noop',parameters:{type:'object',properties:{},additionalProperties:false}}]}];
 const f=fixture(tools);try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await exec('child-begin',a),before=p.counters.exec;
  const first=await exec('child-view-read',{...a,args:{...acquisitionArgs(begin)}});assert.equal(first.ok,true);assert.equal(first.acquisition_chunk_count,4);assert.ok(first.acquisition_bytes<=16384);assert.equal(first.emitted_blocks.filter(x=>typeof x==='string').length,4);assert.equal(p.counters.exec-before,4);
  const rendered=first.emitted_blocks.reduce((sum,x)=>sum+Buffer.byteLength(typeof x==='string'?x:JSON.stringify({...x,emitted_blocks:undefined,text:undefined})),0);assert.ok(rendered<=28672);
  const beforeReread=fs.readFileSync(path.join(a.stateDir,'journal.json'));const repeat=await exec('child-view-read',{...a,args:{...acquisitionArgs(begin)}});assert.equal(repeat.text,first.text);assert.ok(fs.readFileSync(path.join(a.stateDir,'journal.json')).equals(beforeReread));
  let offset=first.next_offset,raw=first.text,last=first,cells=1;while(!last.complete){last=await exec('child-view-read',{...a,args:{...acquisitionArgs(begin),offset}});assert.equal(last.ok,true);assert.equal(last.offset,offset);assert.ok(last.acquisition_chunk_count<=4);raw+=last.text;offset=last.next_offset;cells++;}
  assert.ok(last.acquisition_chunk_count<4);assert.ok(cells<Math.ceil(begin.model_view_byte_length/4096));assert.equal(Buffer.byteLength(raw),begin.model_view_byte_length);assert.equal(require('node:crypto').createHash('sha256').update(raw).digest('hex'),begin.model_view_sha256);assert.equal(JSON.parse(raw).request.tools[0].description,namespaceText);
 }finally{f.cleanup();}
});

test('batch preserves prior issued chunks on mid-batch failure and enforces an aggregate emission bound',async()=>{
 const large='namespace instructions '.repeat(1400),f=fixture([{type:'namespace',name:'functions',description:large,tools:[]}]);
 try{const p=fakeTools(f),exec=makeExecutor(f,p),a=await admit(f,exec),begin=await exec('child-begin',a),base=p.tools.exec_command;let chunks=0;
  p.tools.exec_command=async args=>{if(args.cmd.includes("'--operation' 'child-view-read'") && ++chunks===2)return {exit_code:0,output:JSON.stringify({ok:false,error:{code:'request_view_binding_mismatch'}})};return base(args);};
  const partial=await exec('child-view-read',{...a,args:acquisitionArgs(begin)});assert.equal(partial.ok,false);assert.equal(partial.error.code,'request_view_binding_mismatch');assert.equal(partial.acquisition_chunk_count,1);assert.equal(Buffer.byteLength(partial.text),4096);
  p.tools.exec_command=base;const first=await exec('child-view-read',{...a,args:{...acquisitionArgs(begin),maxChunks:1}});assert.equal(first.text,partial.text);assert.equal(first.acquisition_chunk_count,1);
  const resumed=await exec('child-view-read',{...a,args:{...acquisitionArgs(begin),offset:first.next_offset}});assert.equal(resumed.ok,true);assert.equal(resumed.offset,4096);
  const adapter=createLiteNativeAdapter({}, {...captureConfig,captureSink:()=>true}),out=[];
  adapter.present({ok:true,offset:0,acquisition_chunks:[{offset:0,next_offset:4096,total_bytes:4096,sha256:'a'.repeat(64),complete:true,text:'x'.repeat(4096),binding:{oversized:'x'.repeat(30000)}}]},value=>out.push(value));
  assert.equal(out.length,1);assert.equal(out[0].error.code,'lite_acquisition_output_budget');assert.equal(out[0].content_emitted,false);assert.equal(out[0].execution_permit,false);assert.deepEqual(out[0].acquisition_retry,{offset:0,maxChunks:1,maxBytes:2048});
 }finally{f.cleanup();}
});
