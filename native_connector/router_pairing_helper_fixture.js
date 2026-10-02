'use strict';
/* Actual trusted adapter -> actual Python CLI, synthetic connector ports only. */
const fs=require('node:fs');
const cp=require('node:child_process');
const util=require('node:util');
const assert=require('node:assert/strict');
const {bodyForText,textOf:documentText}=require('./indexed_docs_test_support');
const run=util.promisify(cp.exec);
global.createNativeToolAdapter=require('./tool_adapter').createNativeToolAdapter;
const {createRouterPairingToolAdapter,createRouterPairingRunner}=require('./router_pairing');
const scenario=JSON.parse(fs.readFileSync(process.argv[2]));
const docs=scenario.documents,files=scenario.files,counts={docs:0,metadata:0,fetch:0,download:0,upload:0,cas:0,helpers:0};
let serial=0;const errors=[];const operations={};let truncated=false;let conflictSnapshot=null;const readIds=[];
// The shared Python provider now emits real paragraphs and split UTF-16 runs.
// Read the complete child block; synthetic mutations must rebuild every index.
const tabIdOf=doc=>doc.tabs[0].tabProperties?.tabId??doc.tabs[0].tabId;
const textOf=doc=>documentText(doc,tabIdOf(doc));
const setText=(doc,text)=>{
  const tab=doc.tabs[0],target=tab.documentTab??tab;
  target.body=bodyForText(text,{runSize:701});
};
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
      const parts=textOf(doc).split('\n'),state=JSON.parse(parts[1]);
      state.stage='BUNDLE_READY';state.control.document_id='unapproved-hint-destination';
      parts[1]=JSON.stringify(state);setText(doc,parts.join('\n'));
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
        const input=privateFile('abort-peer.json',JSON.stringify({text:textOf(doc),
          handoff:scenario.config.handoff.path,mode:'abort'}));
        const out=cp.execFileSync('python3',['-B','-m','remote_tests.router_pairing_peer_fixture',input],
          {cwd:scenario.config.cwd,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
        setText(doc,JSON.parse(out.toString()).bootstrap);
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
    assert.equal(requests.length,1,'child protocol retains one replacement');
    assert.deepEqual(Object.keys(requests[0]),['replaceAllText']);
    const replace=requests[0].replaceAllText;
    assert.deepEqual(Object.keys(replace).sort(),['containsText','replaceText','tabsCriteria']);
    assert.deepEqual(Object.keys(replace.containsText).sort(),['matchCase','searchByRegex','text']);
    assert.equal(replace.containsText.matchCase,true);assert.equal(replace.containsText.searchByRegex,false);
    assert.deepEqual(replace.tabsCriteria,{tabIds:[tabIdOf(doc)]});
    assert.equal(typeof replace.containsText.text,'string');assert(replace.containsText.text.length>0);
    assert.equal(typeof replace.replaceText,'string');
    const parts=textOf(doc).split(replace.containsText.text);
    assert.equal(parts.length,2,'exact whole child block replacement required');
    // Literal replacement: do not let JavaScript treat "$&" as interpolation.
    setText(doc,parts.join(replace.replaceText));
    doc.revisionId='r'+(Number(doc.revisionId.slice(1))+1);
    const response={documentId:document_id,replies:[{replaceAllText:{occurrencesChanged:1}}],
      writeControl:{requiredRevisionId:doc.revisionId}};
    if(counts.cas===1&&scenario.advance!==false) {
      const probe=Object.values(files).find(f=>f.id.startsWith('reverse'));
      const input=privateFile('peer.json',JSON.stringify({text:textOf(doc),probe,handoff:scenario.config.handoff.path}));
      const out=cp.execFileSync('python3',['-B','-m','remote_tests.router_pairing_peer_fixture',input],
        {cwd:scenario.config.cwd,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1'}});
      const published=JSON.parse(out.toString());
      setText(doc,published.bootstrap);doc.revisionId='r'+(Number(doc.revisionId.slice(1))+1);
      const control=docs[scenario.config.controlDocumentId];setText(control,published.control);
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
