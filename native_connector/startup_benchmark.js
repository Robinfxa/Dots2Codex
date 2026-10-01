/* Offline startup boundary benchmark. Real local helpers; synthetic Docs only.
 * Injected delay is deterministic virtual time, NOT connector/startup/TTFT data.
 * Usage: node startup_benchmark.js [--baseline /path/to/previous/source]
 * Public output contains aggregate counts only, never payloads or identifiers.
 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {virtualClock}=require('./test_support');
const execFileAsync=promisify(execFile);
const fixtureScript=path.resolve(__dirname,'../remote_tests/startup_benchmark_fixture.py');
const DEFAULT_LATENCIES=Object.freeze({docs_read:3000,docs_write:5000,helper_exec:1000});

async function runScenario(repo,scenario,{latencies=DEFAULT_LATENCIES,loseWrite=false,
    failReadback=false,conflictOnWrite=0}={}) {
  repo=path.resolve(repo);
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'startup-offline-'));
  fs.chmodSync(root,0o700);
  const clock=virtualClock(),counts={docs_reads:0,docs_writes:0,helper_execs:0,
    capture_execs:0,result_chunk_execs:0,check_execs:0,verify_execs:0,
    plan_execs:0,inspect_execs:0,same_second_execs:0,
    inline_check_execs:0,first_chunk_execs:0,native_plan_execs:0,inline_native_check_execs:0};
  let maxPlanChunks=0,currentPlanChunks=0,readAfterWrite=false,writeLost=false;
  try {
    await execFileAsync('python3',['-B',fixtureScript,'--root',root,'--scenario',scenario],
      {cwd:repo,maxBuffer:1024*1024});
    const config=JSON.parse(fs.readFileSync(path.join(root,'fixture.json'),'utf8'));
    config.nativePlanFile=path.join(config.stateDir,'preselected-native-plan.json');
    let document=JSON.parse(fs.readFileSync(path.join(root,'document.json'),'utf8'));
    const tools={
      async exec_command({cmd,workdir}) {
        assert.equal(workdir,repo);
        // These commands come only from the reviewed adapter under test.
        assert.match(cmd,/(?:remote_transport\.(?:global_native|connector_files)|import time; time.sleep)/);
        counts.helper_execs++;
        if(cmd.includes("'capture'")||cmd.includes("'capture-begin'")||
            cmd.includes("'capture-append'")||cmd.includes("'capture-seal'"))counts.capture_execs++;
        if(cmd.includes("'--result-first-chunk'")){
          currentPlanChunks=1;maxPlanChunks=Math.max(maxPlanChunks,currentPlanChunks);
        }else if(cmd.includes("'packet-chunk'")){
          counts.result_chunk_execs++;currentPlanChunks++;
          maxPlanChunks=Math.max(maxPlanChunks,currentPlanChunks);
        }else currentPlanChunks=0;
        if(cmd.includes("'check-cas'"))counts.check_execs++;
        if(cmd.includes("'--check-cas-now'"))counts.inline_check_execs++;
        if(cmd.includes("'plan-native'"))counts.native_plan_execs++;
        if(cmd.includes("'--check-native-now'"))counts.inline_native_check_execs++;
        if(cmd.includes("'--result-first-chunk'"))counts.first_chunk_execs++;
        if(cmd.includes("'verify'"))counts.verify_execs++;
        if(/'plan-(?:join|join-heartbeat|heartbeat|claim|claim-begin|claim-startup|begin|admitted|heartbeat-admitted)'/.test(cmd))counts.plan_execs++;
        if(/'inspect(?:-heartbeat)?'/.test(cmd))counts.inspect_execs++;
        if(cmd.includes('time.sleep'))counts.same_second_execs++;
        await clock.wait(latencies.helper_exec);
        try {
          const result=await execFileAsync('/bin/bash',['-c',cmd],{cwd:repo,maxBuffer:16*1024*1024});
          return {exit_code:0,output:result.stdout};
        } catch(error) {
          // The adapter consumes exact private output; never surface it publicly.
          return {exit_code:typeof error.code==='number'?error.code:1,output:error.stdout||''};
        }
      },
      async mcp__codex_apps__google_drive_get_document({document_id}) {
        counts.docs_reads++;await clock.wait(latencies.docs_read);
        assert.equal(document_id,document.documentId);
        if(failReadback&&readAfterWrite)throw Object.assign(Error('synthetic readback failure'),{status:503});
        return {structuredContent:structuredClone(document)};
      },
      async mcp__codex_apps__google_drive_batch_update_document(args) {
        counts.docs_writes++;await clock.wait(latencies.docs_write);
        assert.equal(args.document_id,document.documentId);
        if(counts.docs_writes===conflictOnWrite){
          // Concurrent unrelated provider revision after the prior readback.
          // Keep the signed content unchanged so exact-event reconciliation
          // must reject the uncommitted operation, never replay its write.
          document.revisionId='r'+(Number(document.revisionId.slice(1))+1);
          throw Object.assign(Error('requiredRevisionId revision mismatch'),{status:409});
        }
        assert.deepEqual(args.write_control,{requiredRevisionId:document.revisionId});
        assert.equal(args.requests.length,1);
        const replacement=args.requests[0].replaceAllText;
        assert(replacement);assert.equal(replacement.tabsCriteria.tabIds[0],config.tabId);
        const run=document.tabs[0].documentTab.body.content[1].paragraph.elements[0].textRun;
        const old=replacement.containsText.text;
        const occurrences=run.content.split(old).length-1;
        assert.equal(occurrences,1);
        run.content=run.content.replace(old,replacement.replaceText);
        document.revisionId='r'+(Number(document.revisionId.slice(1))+1);
        readAfterWrite=true;
        if(loseWrite&&!writeLost){writeLost=true;throw Object.assign(Error('synthetic lost response'),{code:'ETIMEDOUT'});}
        return {structuredContent:{documentId:document.documentId,
          replies:[{replaceAllText:{occurrencesChanged:occurrences}}],
          writeControl:{requiredRevisionId:document.revisionId}}};
      }
    };
    const {createNativeToolAdapter}=require(path.join(repo,'native_connector/tool_adapter.js'));
    const {createGlobalControllerToolAdapter}=require(path.join(repo,'native_connector/global_controller_cell.js'));
    const capture=createNativeToolAdapter(tools,{cwd:repo,root:config.stateDir,nativeTaskId:config.nativeTaskId});
    const adapter=createGlobalControllerToolAdapter(tools,config,capture.captureValue);
    const start=process.hrtime.bigint();let outcome='verified';
    try {
      if(scenario==='join'){
        const result=await adapter.cell.joinAndFirstHeartbeat({capacity:2,seconds:1200});
        assert.equal(result.verified,true);assert.equal(result.first_heartbeat_required,false);
      }else if(scenario==='claim_prepare_native'){
        const result=await adapter.cell.claimAndPrepareNative({routeId:config.routeId});
        assert.equal(result.claim_verified,true);assert.equal(result.begin_verified,true);
        assert.equal(result.native_spawn_invoked_by_cell,false);
        assert.equal(result.next_direct_platform_tool_required,true);
        assert.equal(result.plan_file,config.nativePlanFile);
        const plan=JSON.parse(fs.readFileSync(result.plan_file,'utf8'));
        assert.deepEqual(result.arguments,plan.arguments);
        assert.equal(result.execute_before,plan.execute_before);
        assert(result.execute_before>result.checked_at);
        assert(result.execute_before<=plan.created+10);
        assert.deepEqual(JSON.parse(fs.readFileSync(result.record_snapshot_file,'utf8')),document);
      }else if(scenario==='claim_begin'||scenario==='claim_begin_due'){
        const result=await adapter.cell.claimAndBegin({routeId:config.routeId});
        assert.equal(result.claim_verified,true);assert.equal(result.begin_verified,true);
        assert.equal(result.native_spawn_prepared,false);
      }else {
        assert.equal(typeof adapter.cell.completeAdmission,'function');
        const result=await adapter.cell.completeAdmission({...config,
          recordSnapshotFile:path.join(root,'document.json')});
        assert.equal(result.imported,true);assert.equal(result.admitted_verified,true);
      }
    } catch(error) {
      if(!failReadback&&!conflictOnWrite)throw error;
      assert.match(error.message,/global_cas_outcome_unknown_no_replay/);
      outcome=counts.docs_writes===1?'failed_closed_after_first_write':'failed_closed_after_second_write';
    }
    const actualLocalMs=Number(process.hrtime.bigint()-start)/1e6;
    fs.writeFileSync(path.join(root,'final-document.json'),JSON.stringify(document),{mode:0o600});
    const audited=await execFileAsync('python3',['-B',fixtureScript,'--root',root,'--scenario',scenario,'--audit-final'],
      {cwd:repo,maxBuffer:1024*1024});
    const evidence=JSON.parse(audited.stdout);
    if(outcome==='verified'){
      assert.equal(evidence.authenticated,true);
      assert.equal(evidence.unresolved_operation_count,0);
      assert.deepEqual(evidence.new_event_kinds,scenario==='join'?['join','heartbeat']:
        scenario==='claim_begin_due'?['heartbeat','claim','begin']:
        scenario.startsWith('claim_')?['claim','begin']:['heartbeat','admitted']);
      assert.equal(evidence.distinct_reserved_plan_count,counts.docs_writes);
      assert.equal(evidence.reservation_count,evidence.shared_plan?1:2);
      assert.equal(evidence.native_spawn_reservations,['admission','claim_prepare_native'].includes(scenario)?1:0);
    }else{
      assert(evidence.unresolved_operation_count>0);
      assert.equal(evidence.native_spawn_reservations,scenario==='admission'?1:0);
      assert.equal(evidence.child_imported,false);
    }
    return {scenario,outcome,counts,total_rpc_calls:counts.docs_reads+counts.docs_writes+counts.helper_execs,
      signed_state_evidence:evidence,
      maximum_result_chunks_per_helper:maxPlanChunks,
      injected_rpc_critical_path_ms:clock.now(),
      sum_injected_rpc_delay_ms:counts.docs_reads*latencies.docs_read+counts.docs_writes*latencies.docs_write+
        counts.helper_execs*latencies.helper_exec,
      local_wall_ms_excluding_fixture:Math.round(actualLocalMs),
      native_dispatches:0,external_network_calls:0};
  }finally{fs.rmSync(root,{recursive:true,force:true});}
}

function compare(before,after){
  assert.equal(before.scenario,after.scenario);
  assert.equal(before.outcome,after.outcome);
  let groupedCasReduction=false;
  if(before.counts.docs_writes!==after.counts.docs_writes){
    // A write may disappear only when both original signed events and their
    // exact group reservation have been authenticated in the one atomic plan.
    assert(['join','claim_begin','claim_begin_due','admission'].includes(before.scenario));
    assert.equal(before.outcome,'verified');
    assert.equal(before.counts.docs_writes,2);assert.equal(after.counts.docs_writes,1);
    const pairs={join:['join','heartbeat'],claim_begin:['claim','begin'],
      claim_begin_due:['heartbeat','claim','begin'],admission:['heartbeat','admitted']};
    const stages={join:null,claim_begin:'spawn_intent',claim_begin_due:'spawn_intent',admission:'admitted'};
    for(const result of [before,after]){
      const proof=result.signed_state_evidence;
      assert.equal(proof.authenticated,true);
      assert.deepEqual(proof.new_event_kinds,pairs[before.scenario]);
      assert.deepEqual(proof.verified_operation_kinds,[...pairs[before.scenario]].sort());
      assert.equal(proof.unresolved_operation_count,0);
      assert.equal(proof.reservation_count,result.counts.docs_writes);
      assert.equal(proof.distinct_reserved_plan_count,result.counts.docs_writes);
      assert.equal(proof.route_state,stages[before.scenario]);
      assert.equal(proof.native_spawn_reservations,before.scenario==='admission'?1:0);
      if(before.scenario==='admission')assert.equal(proof.child_imported,true);
    }
    const flag={join:'join_heartbeat_shared_plan',claim_begin:'claim_begin_shared_plan',
      claim_begin_due:'heartbeat_claim_begin_shared_plan',admission:'heartbeat_admitted_shared_plan'}[before.scenario];
    assert.equal(after.signed_state_evidence[flag],true);
    groupedCasReduction=true;
  }
  return {scenario:before.scenario,before,after,
    grouped_cas_reduction_verified:groupedCasReduction,
    saved_rpc_calls:before.total_rpc_calls-after.total_rpc_calls,
    saved_provider_rpc_calls:before.counts.docs_reads+before.counts.docs_writes-
      after.counts.docs_reads-after.counts.docs_writes,
    saved_helper_rpc_calls:before.counts.helper_execs-after.counts.helper_execs,
    saved_injected_rpc_delay_ms:before.injected_rpc_critical_path_ms-after.injected_rpc_critical_path_ms};
}

async function measureNativePayload(repo){
  repo=path.resolve(repo);
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'startup-payload-'));
  fs.chmodSync(root,0o700);
  try{
    await execFileAsync('python3',['-B',fixtureScript,'--root',root,'--scenario','admission'],
      {cwd:repo,maxBuffer:1024*1024});
    const config=JSON.parse(fs.readFileSync(path.join(root,'fixture.json'),'utf8'));
    const args=JSON.parse(fs.readFileSync(config.actualArgumentsFile,'utf8'));
    const plan=JSON.parse(fs.readFileSync(config.planFile,'utf8'));
    assert.equal(typeof args.message,'string');
    return {message_utf8_bytes:Buffer.byteLength(args.message),
      serialized_spawn_arguments_utf8_bytes:Buffer.byteLength(JSON.stringify(args)),
      compact_private_handoff:!!plan.handoff,native_dispatches:0,
      note:'Serialized bytes only; not token count, model latency, or live admission.'};
  }finally{fs.rmSync(root,{recursive:true,force:true});}
}

async function benchmark({candidate=path.resolve(__dirname,'..'),baseline=null}={}){
  const scenarios=[];
  for(const scenario of ['join','claim_begin','admission']){
    const before=baseline?await runScenario(baseline,scenario):null;
    const after=await runScenario(candidate,scenario);
    scenarios.push(before?compare(before,after):after);
  }
  const nativePayload={before:baseline?await measureNativePayload(baseline):null,
    after:await measureNativePayload(candidate)};
  if(nativePayload.before)nativePayload.saved_message_utf8_bytes=
    nativePayload.before.message_utf8_bytes-nativePayload.after.message_utf8_bytes;
  const dueAfter=await runScenario(candidate,'claim_begin_due');
  const dueClaim=baseline?compare(await runScenario(baseline,'claim_begin_due'),dueAfter):dueAfter;
  const nativePreparation=await runScenario(candidate,'claim_prepare_native');
  const failureScenarios=[];
  for(const scenario of ['join','claim_begin','claim_begin_due','claim_prepare_native','admission']){
    for(const [fault,options] of [['lost_response',{loseWrite:true}],
      ['unreadable_outcome',{loseWrite:true,failReadback:true}],
      ['revision_conflict',{conflictOnWrite:1}]]){
      failureScenarios.push({fault,...await runScenario(candidate,scenario,options)});
    }
  }
  return {mode:'offline_real_local_helpers_synthetic_docs_virtual_rpc_latency',
    injected_latency_ms:DEFAULT_LATENCIES,scenarios,native_payload:nativePayload,
    due_heartbeat_claim_comparison:dueClaim,
    candidate_claim_and_native_preparation:nativePreparation,
    failure_scenarios:failureScenarios,
    claims:{live_startup_observed:false,true_ttft_observed:false,sub_180_seconds_verified:false,
      model_latency_included:false,native_scheduling_included:false,connector_latency_observed:false},
    limits:['RPC delay is injected virtual time, not measured provider latency.',
      'The local helper host clock remains real; virtual delays do not test expiration budgets.',
      'Synthetic fixtures cover one empty startup and one route, not queue-size distributions.',
      'Facade meaningful response content remains gated on committed result; no true token-streaming claim.']};
}

if(require.main===module){
  const args=process.argv.slice(2),at=args.indexOf('--baseline');
  benchmark({baseline:at<0?null:args[at+1]}).then(value=>process.stdout.write(JSON.stringify(value,null,2)+'\n'))
    .catch(()=>{process.stderr.write('offline_startup_benchmark_failed; inspect private test diagnostics\n');process.exitCode=1;});
}
module.exports={runScenario,benchmark,compare,measureNativePayload,DEFAULT_LATENCIES};
