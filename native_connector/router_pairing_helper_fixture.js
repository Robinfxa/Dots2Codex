'use strict';
/* Actual trusted adapter -> actual Python CLI, synthetic connector ports only. */
const fs=require('node:fs');
const cp=require('node:child_process');
const util=require('node:util');
const run=util.promisify(cp.exec);
global.createNativeToolAdapter=require('./tool_adapter').createNativeToolAdapter;
const {createRouterPairingToolAdapter,createRouterPairingRunner}=require('./router_pairing');
const scenario=JSON.parse(fs.readFileSync(process.argv[2]));
const docs=scenario.documents,files=scenario.files,counts={docs:0,metadata:0,fetch:0,download:0,upload:0,cas:0,helpers:0};
let serial=0;const errors=[];const operations={};let truncated=false;let conflictSnapshot=null;const readIds=[];
const textOf=doc=>doc.tabs[0].documentTab.body.content[1].paragraph.elements[0].textRun;
const privateFile=(name,raw)=>{const p=scenario.directory+'/'+name;fs.writeFileSync(p,raw,{mode:0o600});return p;};
const tools={
  async exec_command({cmd,workdir}) {
    counts.helpers++;
    const match=cmd.match(/'remote_transport\.router_pairing' '([^']+)'/);
    if(match)operations[match[1]]=(operations[match[1]]||0)+1;
    try{const out=await run(cmd,{cwd:workdir,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'},maxBuffer:8*1024*1024});
      if(scenario.truncateResult&&!truncated&&cmd.includes("'--result-large-chunk'")) {
        truncated=true;return {exit_code:0,output:out.stdout.slice(0,31)};
      }
      return {exit_code:0,output:out.stdout};}
    catch(error){errors.push({stdout:error.stdout,stderr:error.stderr});return {exit_code:error.code||1,output:error.stdout||'',stderr:error.stderr};}
  },
  async mcp__codex_apps__google_drive_get_document({document_id}) {
    counts.docs++;readIds.push(document_id);
    const doc=structuredClone(docs[document_id]);
    if(scenario.forgeBundleHint&&document_id===scenario.config.documentId&&counts.cas===1) {
      const run=textOf(doc),parts=run.content.split('\n'),state=JSON.parse(parts[1]);
      state.stage='BUNDLE_READY';state.control.document_id='unapproved-hint-destination';
      parts[1]=JSON.stringify(state);run.content=parts.join('\n');
    }
    return {structuredContent:doc};
  },
  async mcp__codex_apps__google_drive_get_file_metadata({fileId}) {
    counts.metadata++;const file=files[fileId];
    if(scenario.forwardFailure&&fileId===scenario.forwardId)throw Error('synthetic forward failure');
    return {structuredContent:{id:fileId,title:file.name,mime_type:'application/json',parent_ids:file.parents,
      url:'https://drive.google.com/file/d/'+fileId+'/view'}};
  },
  async mcp__codex_apps__google_drive_fetch({url,download_raw_file,include_base64}) {
    counts.fetch++;if(download_raw_file!==true||include_base64!==false)throw Error('raw flags required');
    const id=url.split('/')[5];if(!files[id])throw Error('unknown raw file');
    return {structuredContent:{id:scenario.wrongRawId&&id===scenario.forwardId?'wrong-id':id,file_uri:{file_id:'sediment://file_'+id}}};
  },
  async download_file({file_id}) {
    counts.download++;const id=file_id.slice(5);
    if(scenario.rawMismatch&&id===scenario.forwardId)fs.writeFileSync(files[id].path,'{}');
    if(scenario.revisionConflict&&counts.download===2) {
      const doc=docs[scenario.config.documentId];
      if(scenario.abortDuringProbes) {
        const input=privateFile('abort-peer.json',JSON.stringify({text:textOf(doc).content,
          handoff:scenario.config.handoff.path,mode:'abort'}));
        const out=cp.execFileSync('python3',['-B','-m','remote_tests.router_pairing_peer_fixture',input],
          {cwd:scenario.config.cwd,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
        textOf(doc).content=JSON.parse(out.toString()).bootstrap;
      }
      doc.revisionId='r'+(Number(doc.revisionId.slice(1))+1);conflictSnapshot=JSON.stringify(doc);
    }
    return {path:files[id].path};
  },
  async mcp__codex_apps__google_drive_upload_file({file_uri,file_name,mime_type,parent_folder_id}) {
    counts.upload++;if(mime_type!=='application/json')throw Error('mime');
    const id='reverse'+(++serial);
    const path=privateFile(id+'.json',fs.readFileSync(file_uri));
    files[id]={id,path,name:file_name,parents:[parent_folder_id]};
    return {structuredContent:{success:true,id}};
  },
  async mcp__codex_apps__google_drive_batch_update_document({document_id,requests,write_control}) {
    counts.cas++;const doc=docs[document_id];
    if(write_control.requiredRevisionId!==doc.revisionId){counts.rejectedCas=(counts.rejectedCas||0)+1;throw Error('synthetic CAS conflict');}
    const replace=requests[0].replaceAllText;
    const text=textOf(doc);if(text.content.split(replace.containsText.text).length!==2)throw Error('exact replace');
    text.content=text.content.replace(replace.containsText.text,replace.replaceText);
    doc.revisionId='r'+(Number(doc.revisionId.slice(1))+1);
    const response={documentId:document_id,replies:[{replaceAllText:{occurrencesChanged:1}}],
      writeControl:{requiredRevisionId:doc.revisionId}};
    if(counts.cas===1&&scenario.advance!==false) {
      const probe=Object.values(files).find(f=>f.id.startsWith('reverse'));
      const input=privateFile('peer.json',JSON.stringify({text:text.content,probe,handoff:scenario.config.handoff.path}));
      const out=cp.execFileSync('python3',['-B','-m','remote_tests.router_pairing_peer_fixture',input],
        {cwd:scenario.config.cwd,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
      const published=JSON.parse(out.toString());
      text.content=published.bootstrap;doc.revisionId='r'+(Number(doc.revisionId.slice(1))+1);
      const control=docs[scenario.config.controlDocumentId];textOf(control).content=published.control;
      control.revisionId='r'+(Number(control.revisionId.slice(1))+1);
    }
    return {structuredContent:response};
  }
};
(async()=>{
  const adapter=createRouterPairingToolAdapter(tools,scenario.config);
  if(scenario.forceHintFalse)adapter.io.mayHaveBundle=()=>false;
  const runner=createRouterPairingRunner(adapter.io);
  const outcome=await runner.pairOnce();
  const second=scenario.repeatPair?await runner.pairOnce():null;
  console.log(JSON.stringify({outcome,second,counts,errors,operations,truncated,readIds,
    conflictUnchanged:conflictSnapshot===null?null:conflictSnapshot===JSON.stringify(docs[scenario.config.documentId])}));
})().catch(error=>{console.error(error);process.exitCode=1;});
