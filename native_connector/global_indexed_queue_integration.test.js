'use strict';
// Actual generated functions.exec source + actual Python protocol helpers.
// Every connector is a synthetic local port; no native dispatch or live writes.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {reflowDocument,textOf,applyDocumentBatch}=require('./indexed_docs_test_support');
const run=promisify(execFile),repo=path.resolve(__dirname,'..');
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;

async function exercise({minimumStateBytes=24576,runSize=701,fault=null,mutateSource=null,normalized=false}={}){
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'indexed-generated-offline-'));fs.chmodSync(root,0o700);
  try{
    await run('python3',['-B','remote_tests/startup_benchmark_fixture.py','--root',root,
      '--scenario','claim_begin','--minimum-state-bytes',String(minimumStateBytes)],{cwd:repo,maxBuffer:1024*1024});
    const config=JSON.parse(fs.readFileSync(path.join(root,'fixture.json'),'utf8'));
    let document=reflowDocument(JSON.parse(fs.readFileSync(path.join(root,'document.json'),'utf8')),config.tabId,{runSize});
    if(normalized){
      const tab=document.tabs[0];document.body=null;
      document.tabs=[{...tab.tabProperties,...tab.documentTab,parentTabId:null,nestingLevel:null,
        documentId:document.documentId,document_url:null}];
    }
    const sourceText=textOf(document,config.tabId),initialDocument=structuredClone(document);
    assert(Buffer.byteLength(sourceText)>=minimumStateBytes);
    const paragraphs=(document.tabs[0].documentTab?.body??document.tabs[0].body).content.slice(1);
    assert(paragraphs.length>=3);assert(paragraphs.some(p=>p.paragraph.elements.length>20));
    const snapshotFile=path.join(root,'generated-source.json'),cellFile=path.join(config.stateDir,'indexed-generated.js');
    fs.writeFileSync(snapshotFile,JSON.stringify(document),{mode:0o600});
    await run('python3',['-B','-m','remote_transport.global_native','emit-cell',
      '--document-id',config.documentId,'--tab-id',config.tabId,'--join-code-file',config.joinCodeFile,
      '--state-dir',config.stateDir,'--native-task-id',config.nativeTaskId,'--snapshot',snapshotFile,
      '--save',cellFile,'--package-root',repo,'--cell-operation','claim-begin','--route-id',config.routeId],
      {cwd:repo,maxBuffer:1024*1024});
    const source=fs.readFileSync(cellFile,'utf8');assert(source.includes('activeController.cell.claimAndBegin'));
    const requests=[],commands=[],output=[];let reads=0;
    if(mutateSource)mutateSource(document);
    const tools={
      async exec_command({cmd,workdir}){
        assert.equal(workdir,repo);assert.match(cmd,/remote_transport\.(global_native|connector_files)/);commands.push(cmd);
        try{const result=await run('/bin/bash',['-c',cmd],{cwd:repo,maxBuffer:16*1024*1024});
          return {exit_code:0,output:result.stdout};}
        catch(error){return {exit_code:typeof error.code==='number'?error.code:1,output:error.stdout||''};}
      },
      async mcp__codex_apps__google_drive_get_document({document_id}){
        assert.equal(document_id,config.documentId);reads++;
        if(fault==='unreadable'&&requests.length)throw Object.assign(Error('synthetic unreadable outcome'),{status:503});
        if(fault==='stale_readback'&&requests.length)return {structuredContent:structuredClone(initialDocument)};
        return {structuredContent:structuredClone(document)};
      },
      async mcp__codex_apps__google_drive_batch_update_document(args){
        requests.push(structuredClone(args));assert.equal(requests.length,1,'CAS must never replay');
        assert.equal(args.requests.length,2);assert(!JSON.stringify(args.requests).includes('replaceAllText'));
        assert.deepEqual(args.write_control,{requiredRevisionId:document.revisionId});
        assert.deepEqual(args.requests[0],{deleteContentRange:{range:{startIndex:1,endIndex:sourceText.length,tabId:config.tabId}}});
        assert.deepEqual(args.requests[1].insertText.location,{index:1,tabId:config.tabId});
        if(fault==='conflict'){
          document.revisionId+='-concurrent';throw Object.assign(Error('requiredRevisionId conflict'),{status:409});
        }
        const applied=applyDocumentBatch(document,args,{tabId:config.tabId,runSize,allowLegacy:false});
        if(['noop','lost_noop'].includes(fault))document.revisionId=applied.document.revisionId;else document=applied.document;
        if(['lost','lost_noop','unreadable'].includes(fault))throw Object.assign(Error('synthetic committed response lost'),{code:'ETIMEDOUT'});
        if(fault==='stale_response_revision')applied.response.writeControl.requiredRevisionId=initialDocument.revisionId;
        if(fault==='later_readback_revision')document.revisionId+='-later';
        if(fault==='target_revision')applied.response.writeControl={targetRevisionId:document.revisionId};
        if(fault==='missing_write_control')delete applied.response.writeControl;
        if(fault==='same_revision'){
          document.revisionId=initialDocument.revisionId;
          applied.response.writeControl.requiredRevisionId=initialDocument.revisionId;
        }
        if(fault==='one_reply')applied.response.replies=[{}];
        if(fault==='nonempty_reply')applied.response.replies=[{replaceAllText:{occurrencesChanged:1}},{}];
        if(fault==='extra_reply')applied.response.replies=[{},{},{}];
        return {structuredContent:applied.response};
      }
    };
    const invoke=()=>new AsyncFunction('tools','text',source)(tools,value=>output.push(value));
    const success=!fault||['lost','later_readback_revision'].includes(fault);
    if(success){
      await invoke();assert.equal(output.length,1);assert.equal(output[0].claim_verified,true);assert.equal(output[0].begin_verified,true);
    }else await assert.rejects(invoke(),mutateSource?/global_local_helper_failed/:/global_cas_outcome_unknown_no_replay/);
    assert.equal(requests.length,mutateSource?0:1);
    const beforeRetry=requests.length;
    await assert.rejects(invoke());assert.equal(requests.length,beforeRetry,'retry must stop before another write');
    if(mutateSource){
      const saved=JSON.parse(fs.readFileSync(config.ledgerFile,'utf8'));
      assert.equal(Object.values(saved.operations).filter(x=>path.basename(x.path).includes('cell-')).length,0);
      return;
    }
    fs.writeFileSync(path.join(root,'final-document.json'),JSON.stringify(document),{mode:0o600});
    const audit=JSON.parse((await run('python3',['-B','remote_tests/startup_benchmark_fixture.py','--root',root,
      '--scenario','claim_begin','--audit-final'],{cwd:repo,maxBuffer:1024*1024})).stdout);
    assert.equal(audit.authenticated,true);assert.equal(audit.reservation_count,1);assert.equal(audit.distinct_reserved_plan_count,1);
    assert.equal(audit.native_spawn_reservations,0);assert.equal(audit.child_imported,false);
    if(success){
      assert.deepEqual(audit.new_event_kinds,['claim','begin']);assert.equal(audit.unresolved_operation_count,0);
      assert.deepEqual(audit.verified_operation_kinds,['begin','claim']);assert.equal(audit.claim_begin_shared_plan,true);
      assert.equal(textOf(document,config.tabId),requests[0].requests[1].insertText.text+'\n');
      assert.equal(document.revisionId,initialDocument.revisionId+'-next'+(fault==='later_readback_revision'?'-later':''));
    }else{
      assert.equal(audit.unresolved_operation_count,1);assert.deepEqual(audit.verified_operation_kinds,[]);
      if(['noop','lost_noop','conflict'].includes(fault))assert.deepEqual(audit.new_event_kinds,[]);
    }
    assert(reads>=2);assert.equal(commands.filter(c=>c.includes("'plan-claim-startup'")).length,fault==='unreadable'?1:2);
    if(minimumStateBytes>=75000){
      assert(commands.some(c=>c.includes("'capture-begin'")),'large resources must use bounded capture');
      assert(commands.some(c=>c.includes("'packet-chunk'")),'large plan must retain digest-bound chunk transfer');
    }
  }finally{fs.rmSync(root,{recursive:true,force:true});}
}
for(const minimumStateBytes of [24576,75000])
  test('Generated cell commits '+minimumStateBytes+'+ byte signed long multi-run queue by indexed CAS',()=>exercise({minimumStateBytes}));
test('Generated cell accepts verified flattened normalized indexed resource',()=>exercise({normalized:true}));
for(const fault of ['lost','lost_noop','later_readback_revision','noop','conflict','stale_response_revision','same_revision','stale_readback','unreadable',
    'target_revision','missing_write_control',
    'one_reply','nonempty_reply','extra_reply'])
  test('Generated large-queue cell preserves exact verification and no replay: '+fault,()=>exercise({fault}));
for(const [label,mutateSource] of [
  ['missing run index',d=>{delete d.tabs[0].documentTab.body.content[2].paragraph.elements[0].startIndex;}],
  ['overlapping run',d=>{d.tabs[0].documentTab.body.content[2].paragraph.elements[1].startIndex--;}],
  ['paragraph gap',d=>{d.tabs[0].documentTab.body.content[2].startIndex++;}],
])test('Generated cell rejects untrustworthy source topology before reserving CAS: '+label,
  ()=>exercise({fault:'invalid_source',mutateSource}));
