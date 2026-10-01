'use strict';
// Real local Python helpers and immutable ledgers; synthetic Docs, no network,
// native dispatch, provider credentials, or live session activation.
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const run=promisify(execFile);
const {createNativeToolAdapter}=require('./tool_adapter');
const {createGlobalControllerToolAdapter}=require('./global_controller_cell');
const repo=path.resolve(__dirname,'..');

test('Real combined claim and preparation uses exact accepted readback, final native check, and no second reservation',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'native-prepare-offline-'));fs.chmodSync(root,0o700);
  try{
    await run('python3',['-B','remote_tests/startup_benchmark_fixture.py','--root',root,'--scenario','claim_begin'],
      {cwd:repo,maxBuffer:1024*1024});
    const config=JSON.parse(fs.readFileSync(path.join(root,'fixture.json'),'utf8'));
    config.nativePlanFile=path.join(config.stateDir,'preselected-native-plan.json');
    let document=JSON.parse(fs.readFileSync(path.join(root,'document.json'),'utf8'));
    const calls=[],commands=[];
    const tools={
      async exec_command({cmd,workdir}){
        calls.push('helper');commands.push(cmd);assert.equal(workdir,repo);
        assert.match(cmd,/remote_transport\.(global_native|connector_files)/);
        try{const result=await run('/bin/bash',['-c',cmd],{cwd:repo,maxBuffer:16*1024*1024});
          return {exit_code:0,output:result.stdout};}
        catch(error){return {exit_code:1,output:error.stdout};}
      },
      async mcp__codex_apps__google_drive_get_document(){calls.push('read');return structuredClone(document);},
      async mcp__codex_apps__google_drive_batch_update_document(args){
        calls.push('write');assert.equal(args.document_id,document.documentId);
        assert.deepEqual(args.write_control,{requiredRevisionId:document.revisionId});assert.equal(args.requests.length,1);
        const replacement=args.requests[0].replaceAllText;
        const run=document.tabs[0].documentTab.body.content[1].paragraph.elements[0].textRun;
        assert.equal(run.content.split(replacement.containsText.text).length-1,1);
        run.content=run.content.replace(replacement.containsText.text,replacement.replaceText);
        document.revisionId+='-next';
        return {documentId:document.documentId,replies:[{replaceAllText:{occurrencesChanged:1}}],
          writeControl:{requiredRevisionId:document.revisionId}};
      }
    };
    const capture=createNativeToolAdapter(tools,{cwd:repo,root:config.stateDir,nativeTaskId:config.nativeTaskId});
    const adapter=createGlobalControllerToolAdapter(tools,config,capture.captureValue);
    const result=await adapter.cell.claimAndPrepareNative({routeId:config.routeId});
    assert.equal(result.claim_verified,true);assert.equal(result.begin_verified,true);
    assert.equal(result.native_spawn_invoked_by_cell,false);assert.equal(result.next_direct_platform_tool_required,true);
    assert.equal(result.execute_before>result.checked_at,true);
    assert.equal(result.plan_file,config.nativePlanFile);
    const plan=JSON.parse(fs.readFileSync(result.plan_file,'utf8'));
    assert.deepEqual(result.arguments,plan.arguments);assert.equal(result.execute_before,plan.execute_before);
    assert(result.execute_before<=plan.created+10);
    assert.deepEqual(JSON.parse(fs.readFileSync(result.record_snapshot_file,'utf8')),document);
    assert.equal(calls.filter(c=>c==='read').length,2);assert.equal(calls.filter(c=>c==='write').length,1);
    assert.match(commands.at(-1),/'plan-native'/);assert.match(commands.at(-1),/'--check-native-now'/);
    assert(commands.filter(c=>c.includes("'--inline-evidence'")).length>=2);
    const issued=JSON.parse(fs.readFileSync(config.ledgerFile,'utf8'));
    assert.equal(Object.keys(issued.spawns).length,1);
    assert.equal(issued.spawns[config.routeId].status,'reserved_outcome_unknown');
    await assert.rejects(adapter.cell.prepareNative({routeId:config.routeId}),/global_local_helper_failed/);
    const after=JSON.parse(fs.readFileSync(config.ledgerFile,'utf8'));
    assert.equal(Object.keys(after.spawns).length,1);
    assert.deepEqual(after.spawns,issued.spawns);
    assert.equal(calls.filter(c=>c==='write').length,1);
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});
