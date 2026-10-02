'use strict';
// Independent adapter and real-helper operation ledger; all remote/native ports synthetic.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const assert=require('node:assert/strict'),{spawnSync}=require('node:child_process');
const ROOT=__dirname,REPO=process.env.LITE_REPO||path.resolve(ROOT,'..');
const FIXTURES=path.join(REPO,'lite_tests','independent','fixtures');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
let tmp,cwd;
function json(file,value){fs.writeFileSync(file,JSON.stringify(value),{mode:0o600});return file;}
function canon(x){return Array.isArray(x)?'['+x.map(canon).join(',')+']':x&&typeof x==='object'?'{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+canon(x[k])).join(',')+'}':JSON.stringify(x);}
function run(args){const r=spawnSync('python3',['-B',...args],{cwd,encoding:'utf8',maxBuffer:4*1024*1024});assert.equal(r.status,0,r.stderr);return JSON.parse(r.stdout);}
function document(id,text='\n',revision='r0'){
 let cursor=1;const content=[{endIndex:1,sectionBreak:{sectionStyle:{}}}];
 for(const line of text.match(/[^\n]*\n/g)||[]){const start=cursor;cursor+=line.length;content.push({startIndex:start,endIndex:cursor,paragraph:{paragraphStyle:{},elements:[{startIndex:start,endIndex:cursor,textRun:{content:line,textStyle:{}}}]}});}
 return {body:null,documentId:id,revisionId:revision,suggestionsViewMode:'SUGGESTIONS_INLINE',tabs:[{tabId:'t.fixture',index:0,nestingLevel:null,parentTabId:null,body:{content}}]};
}
function text(doc){return doc.tabs[0].body.content.filter(x=>x.paragraph).flatMap(x=>x.paragraph.elements).map(x=>x.textRun.content).join('');}
const seedScript=String.raw`
import json,os,sys,time
from pathlib import Path
from dots_lite import protocol as p
from dots_lite.package_identity import verify_package
root=Path(sys.argv[1]); key='19'*32; now=int(time.time())
g={'protocol':p.PROTOCOL,'activation_id':'audit-activation','folder_id':'fixture-folder','inbox_id':'audit-inbox','created_at':now-1,'expires_at':now+900,'allowed_pairs':[{'model':'gpt-6.1-sol','reasoning_effort':'xhigh'}],'limits':dict(p.DEFAULT_LIMITS),'package_sha256':verify_package()}
request={'stream':True,'model':'gpt-6.1-sol','reasoning':{'effort':'xhigh'},'input':[{'role':'user','content':'Synthetic Unicode 😀 task'}],'tools':[]}
raw=p.canonical(request); desc={'seq':1,'request_id':'request-a1','request_sha256':p.sha256(raw),'byte_length':len(raw),'file_id':'fixture-request-file','folder_id':'fixture-folder','previous_result_ack':None,'begin_before':g['expires_at']}
route={'route_id':'route_a','identity_sha256':'a'*64,'model':'gpt-6.1-sol','reasoning_effort':'xhigh','outbox_id':'audit-outbox','request':desc,'stop':False}
join={'activation_id':g['activation_id'],'inbox_id':g['inbox_id'],'grant_sha256':p.grant_hash(g),'join_code':key}
(root/'join.txt').write_text(p.JOIN_MARKER+' '+p.canonical(join).decode());os.chmod(root/'join.txt',0o600)
print(json.dumps({'inbox':p.make_inbox(g,[route],key,'inbox-op1'),'raw':raw.decode(),'grant':g}))
`;
async function main(){
 tmp=fs.mkdtempSync(path.join(os.tmpdir(),'independent-lite-native-'));fs.chmodSync(tmp,0o700);
 cwd=path.join(tmp,'package');fs.mkdirSync(cwd,{mode:0o700});
 fs.cpSync(path.join(REPO,'dots_lite'),path.join(cwd,'dots_lite'),{recursive:true,filter:x=>!x.includes('__pycache__')});fs.mkdirSync(path.join(cwd,'native_connector'));
 for(const rel of ['native_connector/lite_cell.js','LIGHTWEIGHT.command'])fs.copyFileSync(path.join(REPO,rel),path.join(cwd,rel));
 const entries=fs.readdirSync(path.join(cwd,'dots_lite')).filter(x=>x.endsWith('.py')).map(x=>'dots_lite/'+x).concat(['native_connector/lite_cell.js','LIGHTWEIGHT.command']).sort();
 json(path.join(cwd,'LIGHTWEIGHT_PACKAGE_MANIFEST.json'),{contract:'dots-lite-package/3',files:entries.map(p=>({path:p,sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(cwd,p))).digest('hex')}))});
 const seed=run(['-c',seedScript,tmp]),parent=path.join(tmp,'parent');
 const init=run(['-m','dots_lite.cli','init-parent','--state-dir',parent,'--join-file',path.join(tmp,'join.txt'),'--actor-task-id','/root','--authorization-message-id','synthetic-user-authorization','--available-child-slots','3']);assert.equal(init.ok,true);
 const records=new Map([['audit-inbox',document('audit-inbox',canon(seed.inbox)+'\n')],['audit-outbox',document('audit-outbox')]]),blobs=new Map([['fixture-request-file',Buffer.from(seed.raw)]]);
 const counts={helper:0,source_load:0,docs_get:0,docs_write:0,metadata:0,raw_fetch:0,materialize:0,upload:0,cell_emission:0};
 const operations=[],phases=[];let revision=0,cell=0,nativeBoundary=0,activeRequestId='fixture-request-file';
 const tools={
  async exec_command({cmd,workdir}){assert.equal(workdir,cwd);assert(!cmd.includes('1919191919191919191919191919191919191919191919191919191919191919'));
   const match=cmd.match(/--operation' '([^']+)'/);if(match){counts.helper++;operations.push(match[1]);}else{counts.source_load++;assert(cmd.includes('load-cell'));}
   const r=spawnSync('bash',['-c',cmd],{cwd,encoding:'utf8',maxBuffer:4*1024*1024});return {exit_code:r.status,output:r.stdout};},
  async mcp__codex_apps__google_drive_get_document(args){counts.docs_get++;return {structuredContent:structuredClone(records.get(args.document_id)),isError:false};},
  async mcp__codex_apps__google_drive_batch_update_document(args){counts.docs_write++;const doc=records.get(args.document_id),old=text(doc);
   assert.deepEqual(args.write_control,{requiredRevisionId:doc.revisionId});assert.equal(args.requests.length,old==='\n'?1:2);
   if(old!=='\n')assert.deepEqual(args.requests[0],{deleteContentRange:{range:{startIndex:1,endIndex:old.length,tabId:'t.fixture'}}});
   const ins=args.requests.at(-1).insertText;assert.deepEqual(ins.location,{index:1,tabId:'t.fixture'});const value=ins.text+'\n';
   const record=JSON.parse(value);phases.push(record.phase);records.set(args.document_id,document(args.document_id,value,'r'+(++revision)));
   return {structuredContent:{documentId:args.document_id,revisionId:'r'+revision,replies:args.requests.map(()=>({})),writeControl:{requiredRevisionId:'r'+revision,targetRevisionId:null}},isError:false};},
  async mcp__codex_apps__google_drive_get_file_metadata(args){counts.metadata++;const v=JSON.parse(fs.readFileSync(path.join(FIXTURES,'normalized-file-metadata.json')));Object.assign(v.structuredContent,{id:args.fileId,size:String(blobs.get(args.fileId).length),url:'https://drive.google.com/file/d/'+args.fileId+'/view?usp=drivesdk'});return v;},
  async mcp__codex_apps__google_drive_fetch(args){counts.raw_fetch++;assert.deepEqual(args,{url:'https://drive.google.com/file/d/'+activeRequestId+'/view?usp=drivesdk',download_raw_file:true,include_base64:false});const v=JSON.parse(fs.readFileSync(path.join(FIXTURES,'normalized-raw-fetch.json')));for(const r of [v.structuredContent,v.structuredContent.structuredContent]){r.id=activeRequestId;r.file_size_bytes=blobs.get(activeRequestId).length;}return v;},
  async download_file(args){counts.materialize++;assert.deepEqual(args,{file_id:'file_fixture_raw'});const file=path.join(tmp,'raw-download.json');fs.writeFileSync(file,blobs.get(activeRequestId),{mode:0o644});return {path:file,byte_count:blobs.get(activeRequestId).length};},
  async mcp__codex_apps__google_drive_upload_file(args){counts.upload++;assert.equal(args.parent_folder_id,'fixture-folder');assert.equal(args.mime_type,'application/json');blobs.set('fixture-result-file',fs.readFileSync(args.file_uri));return {structuredContent:{id:'fixture-result-file',success:true}};}
 };
 const cache=new Map();async function execute(action,stateDir=parent,actor='/root',args={}){
  counts.cell_emission++;const file=path.join(tmp,'cell-'+(++cell)+'.js'),argFile=json(path.join(tmp,'args-'+cell+'.json'),args);
  const emitted=run(['-m','dots_lite.cli','emit-cell',action,'--state-dir',stateDir,'--actor-task-id',actor,'--route-id','route_a','--arguments-file',argFile,'--save',file]);assert.equal(emitted.ok,true,JSON.stringify(emitted));let result;
  await new AsyncFunction('tools','load','store','text',fs.readFileSync(file,'utf8'))(tools,k=>cache.get(k),(k,v)=>cache.set(k,v),v=>{result=v});assert.equal(result.ok,true,JSON.stringify(result));return result;
 }
 const reserved=await execute('parent-prepare');assert.equal(reserved.spawn_arguments.fork_turns,'none');assert.equal(reserved.spawn_arguments.reasoning_effort,'xhigh');
 nativeBoundary++;const actual={task_name:'/root/'+reserved.spawn_arguments.task_name,agent_id:'synthetic-actual-boundary-1'};
 const admitted=await execute('parent-admit',parent,'/root',{actualArgumentsFile:json(path.join(tmp,'actual-args.json'),reserved.spawn_arguments),nativeResultFile:json(path.join(tmp,'actual-result.json'),actual)});
 const child=admitted.child_state_dir;nativeBoundary++;await execute('child-takeover',child,actual.task_name,{handoff_file:admitted.handoff_file});
 const before=structuredClone(counts),begin=await execute('child-begin',child,actual.task_name);assert.equal(begin.status,'exposed');
 const output={id:'response-a',object:'response',status:'completed',model:'gpt-6.1-sol',output:[{type:'message',id:'msg-a',role:'assistant',content:[{type:'output_text',text:'Synthetic answer'}]}]};
 const result=await execute('child-complete',child,actual.task_name,{requestId:begin.request_id,outputFile:json(path.join(tmp,'native-output.json'),output)});assert.equal(result.status,'accepted');
 const first=Object.fromEntries(Object.keys(counts).map(k=>[k,counts[k]-before[k]]));
 assert.deepEqual(first,{helper:5,source_load:0,docs_get:1,docs_write:2,metadata:1,raw_fetch:1,materialize:1,upload:1,cell_emission:2});
 assert.deepEqual(phases,['SPAWN_RESERVED','ADMITTED','BEGIN','RESULT']);assert.equal(nativeBoundary,2);
 const artifact=JSON.parse(blobs.get('fixture-result-file'));assert.deepEqual(artifact.output,output);assert.equal(artifact.child_task_id,actual.task_name);
 const cold=structuredClone(counts);
 // Independent synthetic Mac publication of seq2; this is fixture setup, never
 // included as a native helper or presented as a live Google operation.
 const continuation=run(['-c',String.raw`
import json,sys
from dots_lite import protocol as p
inbox=json.loads(sys.argv[1]);request=json.loads(sys.argv[2]);output=json.loads(sys.argv[3]);prior=json.loads(sys.argv[4])
request['input']+=output['output']+[{'role':'user','content':'Next synthetic full-history request'}]
raw=p.canonical(request);route=inbox['routes'][0]
route['request'].update(seq=2,request_id='request-a2',file_id='fixture-request-file-2',request_sha256=p.sha256(raw),byte_length=len(raw),previous_result_ack={'result_id':prior['result_id'],'result_sha256':p.sha256(p.canonical(prior))})
print(json.dumps({'inbox':p.make_inbox(inbox['grant'],[route],'19'*32,'inbox-op2'),'raw':raw.decode()}))
`,JSON.stringify(seed.inbox),seed.raw,JSON.stringify(output),JSON.stringify(artifact)]);
 activeRequestId='fixture-request-file-2';blobs.set(activeRequestId,Buffer.from(continuation.raw));records.set('audit-inbox',document('audit-inbox',canon(continuation.inbox)+'\n','inbox-r2'));
 const warmBefore=structuredClone(counts),warmBegin=await execute('child-begin',child,actual.task_name);
 const warmOutput=structuredClone(output);warmOutput.id='response-b';warmOutput.output[0].id='msg-b';
 await execute('child-complete',child,actual.task_name,{requestId:warmBegin.request_id,outputFile:json(path.join(tmp,'native-output-2.json'),warmOutput)});
 const warm=Object.fromEntries(Object.keys(counts).map(k=>[k,counts[k]-warmBefore[k]]));assert.deepEqual(warm,first);
 assert.equal(nativeBoundary,2);assert.deepEqual(phases,['SPAWN_RESERVED','ADMITTED','BEGIN','RESULT','BEGIN','RESULT']);
 console.log(JSON.stringify({status:'passed',classification:'offline real JS+Python helpers with synthetic connector and native boundaries',cold_from_prepared_inbox:cold,warm_second_request_same_child:warm,actual_native_platform_calls:0,synthetic_spawn_and_handoff_boundaries:nativeBoundary,helper_operations:operations,control_phases:phases,additional_accounting:{parent_initialize:1,actual_argument_result_capture:'not measured: in-process test file write',model_input_read:'not measured: fixture validates file path only',actual_output_file_write:'not measured: in-process synthetic output',google_and_mac_latency:'not measured'}},null,2));
}
require('node:test')('independent real-helper normalized connector operation ledger', async()=>{
 try{await main();}finally{if(tmp)fs.rmSync(tmp,{recursive:true,force:true});}
});
