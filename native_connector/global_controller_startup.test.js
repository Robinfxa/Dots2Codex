'use strict';
// Synthetic ports only. No native dispatch, provider writes, or live activation.
const test=require('node:test');
const assert=require('node:assert/strict');
const {createGlobalControllerCell,createGlobalControllerToolAdapter}=require('./global_controller_cell');
const evidence={routeId:'route-one',planFile:'/private/native-plan.json',
  actualArgumentsFile:'/private/actual-submitted.json',nativeResultFile:'/private/actual-result.json',
  recordSnapshotFile:'/private/exact-pre-spawn-resource.json'};

function fixture({due=[false,false,false]}={}) {
  const calls=[],sources=[],readbacks=[];let epoch=0,dueIndex=0;
  const io={
    async read(){const source=Object.freeze({revision:'provider-revision-'+epoch,epoch});
      calls.push(['read',source]);sources.push(source);return source;},
    async plan(kind,source,extra){calls.push(['plan',kind,source,extra]);
      assert.equal(source.epoch,epoch);return {kind,source,tool_arguments:{kind,revision:source.revision}};},
    async check(plan,source){calls.push(['check',plan.kind,source]);assert.equal(source,plan.source);},
    async write(args){calls.push(['write',args.kind,args]);epoch++;return {epoch};},
    async verify(plan,response,source){calls.push(['verify',plan.kind,source]);
      assert.equal(source.epoch,epoch);readbacks.push(source);
      return {verified:true,first_heartbeat_required:plan.kind==='join',heartbeat_due_at:100+epoch};},
    async heartbeatDue(source){calls.push(['due',source]);return due[dueIndex++];},
    async nextSecond(){calls.push(['nextSecond']);},
    async recordFailure(error,context){calls.push(['failure',context.kind,context.stage]);return '/private/diagnostic';},
    async recordNative(source,actual){calls.push(['record',source,actual]);return {
      native_task_id:'/root/actual-child',admission_receipt_file:'/private/recorded-receipt.json'};},
    async importAdmission(source,{routeId,receipt}){calls.push(['import',source,routeId,receipt]);
      return {imported:true,route_id:routeId,native_task_id:receipt.native_task_id,
        admission_receipt_file:receipt.admission_receipt_file,child_state_dir:'/private/fixed-child'};}
  };
  return {io,cell:createGlobalControllerCell(io),calls,sources,readbacks};
}

test('JOIN group uses one initial authority and reads again on next invocation',async()=>{
  const f=fixture();await f.cell.joinAndFirstHeartbeat({capacity:2,seconds:90});
  const plans=f.calls.filter(c=>c[0]==='plan');
  assert.equal(plans.length,1);assert.equal(plans[0][2],f.sources[0]);assert.equal(f.sources.length,2);
  const old=f.readbacks.at(-1);await f.cell.heartbeat();
  const latest=f.calls.filter(c=>c[0]==='plan').at(-1);
  assert.notEqual(latest[2],old);assert.equal(f.sources.length,4);
});
test('Claim startup group and due trailing heartbeat use one initial read and two exact readbacks',async()=>{
  const f=fixture({due:[true,true,true]});await f.cell.claimAndBegin({routeId:evidence.routeId});
  const plans=f.calls.filter(c=>c[0]==='plan');
  assert.equal(f.sources.length,3);assert.deepEqual(plans.map(c=>c[1]),
    ['claim-startup','heartbeat']);
  assert.equal(plans[0][2],f.sources[0]);
  for(let i=1;i<plans.length;i++)assert.equal(plans[i][2],f.readbacks[i-1]);
  assert.equal(f.calls.filter(c=>c[0]==='write').length,2);
  assert.equal(f.calls.filter(c=>c[0]==='check').length,2);
});
test('Unverified readback cannot be reused even if returned normally',async()=>{
  const f=fixture();f.io.verify=async()=>({verified:false,heartbeat_due_at:0});
  await assert.rejects(f.cell.claimAndBegin({routeId:evidence.routeId}),/global_claim_begin_unverified/);
  assert.equal(f.calls.filter(c=>c[0]==='plan').length,1);
});
test('Clock expiry still checks reused readback before the next write',async()=>{
  const f=fixture({due:[true,false]}),check=f.io.check;
  f.io.check=async(plan,source)=>{await check(plan,source);
    if(plan.kind==='heartbeat')throw Error('global_cas_dispatch_window_expired_no_replay');};
  await assert.rejects(f.cell.claimAndBegin({routeId:evidence.routeId}),/dispatch_window_expired/);
  assert.equal(f.calls.filter(c=>c[0]==='write').length,1);
  assert.equal(f.calls.filter(c=>c[0]==='plan')[1][2],f.readbacks[0]);
});
test('Provider revision conflict on reused readback has one write and no replay or later step',async()=>{
  const f=fixture({due:[true,false]}),write=f.io.write,verify=f.io.verify;
  f.io.write=async args=>{if(args.kind==='heartbeat'){f.calls.push(['write','heartbeat',args]);throw Error('revision conflict');}
    return write(args);};
  f.io.verify=async(plan,...args)=>{if(plan.kind==='heartbeat')throw Error('exact event absent');return verify(plan,...args);};
  await assert.rejects(f.cell.claimAndBegin({routeId:evidence.routeId}),/outcome_unknown_no_replay/);
  assert.deepEqual(f.calls.filter(c=>c[0]==='write').map(c=>c[1]),['claim-startup','heartbeat']);
  assert.equal(f.calls.filter(c=>c[0]==='due').length,1);
});

test('Post-spawn records exact captured evidence first and imports only accepted admitted readback',async()=>{
  const f=fixture(),result=await f.cell.completeAdmission(evidence);
  assert.equal(f.calls[0][0],'record');assert.equal(f.calls[0][1],evidence.recordSnapshotFile);
  assert.deepEqual(f.calls[0][2],{planFile:evidence.planFile,actualArgumentsFile:evidence.actualArgumentsFile,
    nativeResultFile:evidence.nativeResultFile});
  assert.deepEqual(f.calls.filter(c=>c[0]==='write').map(c=>c[1]),['heartbeat-admitted']);
  const plans=f.calls.filter(c=>c[0]==='plan'),imports=f.calls.filter(c=>c[0]==='import');
  assert.equal(plans[0][2],f.sources[0]);assert.equal(plans.length,1);
  assert.equal(imports[0][1],f.readbacks[0]);assert.equal(f.sources.length,2);
  assert.ok(plans.every(c=>c[2]!==evidence.recordSnapshotFile));
  assert.equal(result.imported,true);assert.equal(result.admitted_verified,true);
  assert.equal(result.native_spawn_invoked_by_cell,false);assert.equal(result.heartbeat_due_at,101);
});
test('Post-spawn read failure occurs after durable recording and cannot admit or import',async()=>{
  const f=fixture();f.io.read=async()=>{f.calls.push(['read']);throw Error('read failed');};
  await assert.rejects(f.cell.completeAdmission(evidence),/read failed/);
  assert.deepEqual(f.calls.map(c=>c[0]),['record','read']);
});
test('Post-spawn grouped uncertainty keeps actual record but blocks import',async()=>{
  const f=fixture();f.io.verify=async()=>{throw Error('late heartbeat');};
  await assert.rejects(f.cell.completeAdmission(evidence),/outcome_unknown_no_replay/);
  assert.equal(f.calls[0][0],'record');assert.deepEqual(f.calls.filter(c=>c[0]==='write').map(c=>c[1]),['heartbeat-admitted']);
  assert.equal(f.calls.some(c=>c[0]==='import'),false);
});
test('Post-spawn exact evidence mismatch stops before fresh read or any write',async()=>{
  const f=fixture();f.io.recordNative=async()=>{throw Error('global_actual_native_arguments_mismatch');};
  await assert.rejects(f.cell.completeAdmission(evidence),/global_actual_native_arguments_mismatch/);
  assert.equal(f.calls.length,0);
});
test('Post-spawn admitted uncertainty blocks import without a second write',async()=>{
  const f=fixture(),verify=f.io.verify;
  f.io.verify=async(plan,...args)=>{if(plan.kind==='heartbeat-admitted')throw Error('missing admitted');return verify(plan,...args);};
  await assert.rejects(f.cell.completeAdmission(evidence),/outcome_unknown_no_replay/);
  assert.equal(f.calls.some(c=>c[0]==='import'),false);
  assert.deepEqual(f.calls.filter(c=>c[0]==='write').map(c=>c[1]),['heartbeat-admitted']);
});
test('Post-spawn missing raw paths and mismatched import result fail closed',async()=>{
  const f=fixture();await assert.rejects(f.cell.completeAdmission({...evidence,nativeResultFile:null}),/ports_required/);
  assert.equal(f.calls.length,0);
  f.io.importAdmission=async()=>({imported:true,route_id:'another-route'});
  await assert.rejects(f.cell.completeAdmission(evidence),/global_child_import_unverified/);
});

function adapterFixture({truncated=null}={}) {
  const commands=[],calls=[],captures=[];let packet;
  const config={cwd:'/private/package',stateDir:'/private/state',nativeTaskId:'/root/controller',
    documentId:'doc',tabId:'tab',joinCodeFile:'/private/join'};
  const tools={
    async exec_command({cmd}){
      commands.push(cmd);
      if(cmd.includes("'packet-chunk'")){const text=JSON.stringify(packet);
        return {exit_code:0,output:JSON.stringify({offset_chars:0,next_offset_chars:text.length,
          sha256:'a'.repeat(64),total_chars:text.length,text,eof:true})};}
      if(cmd.includes("'record-native'"))packet={native_task_id:'/root/actual-child',admission_receipt_file:'/private/exact-receipt'};
      else if(cmd.includes("'import-child-admission'"))packet={imported:true,route_id:evidence.routeId,
        native_task_id:'/root/actual-child',admission_receipt_file:'/private/exact-receipt',child_state_dir:'/private/fixed-child'};
      else if(cmd.includes("'check-cas'"))packet={dispatch_allowed:true,checked_at:Date.now()/1000,execute_before:Date.now()/1000+120};
      else if(cmd.includes("'verify'"))packet={verified:true,first_heartbeat_required:false,heartbeat_due_at:Date.now()/1000+25,
        ...(cmd.includes("'--import-child-admission'")?{child_import:{imported:true,route_id:evidence.routeId,
          native_task_id:'/root/actual-child',admission_receipt_file:'/private/exact-receipt',child_state_dir:'/private/fixed-child'}}:{})};
      else if(cmd.includes("'-c'"))packet=null;
      else {const now=Date.now()/1000;packet={plan_file:'/private/cas-plan',operation_id:'test-op',tool_arguments:{document_id:'doc'},
        execute_before:now+120,dispatch_check:{dispatch_allowed:true,checked_at:now,execute_before:now+120}};}
      if(cmd.includes("'--inline-evidence'"))packet={...packet,snapshot_file:'/private/exact-inline-snapshot'};
      const text=JSON.stringify(packet);
      return {exit_code:0,output:truncated&&cmd.includes("'"+truncated+"'")?'{"truncated":':
        cmd.includes("'--result-first-chunk'")?JSON.stringify({offset_chars:0,text,
          sha256:'a'.repeat(64),next_offset_chars:text.length,total_chars:text.length,eof:true}):text};
    },
    async mcp__codex_apps__google_drive_get_document(){calls.push('read');
      return {structuredContent:{documentId:'doc',revisionId:'r'+calls.length,tabs:[]}};},
    async mcp__codex_apps__google_drive_batch_update_document(){calls.push('write');return {documentId:'doc'};}
  };
  const adapter=createGlobalControllerToolAdapter(tools,config,async value=>{
    captures.push(value);return '/private/capture-'+captures.length;});
  return {adapter,commands,calls,captures};
}
test('Adapter uses explicit actual-evidence paths, bounded direct results and no native dispatch tools',async()=>{
  const f=adapterFixture();await f.adapter.cell.completeAdmission(evidence);
  assert.match(f.commands[0],/'record-native'/);
  for(const [flag,path] of [['--actual-arguments',evidence.actualArgumentsFile],['--native-result',evidence.nativeResultFile],
      ['--snapshot',evidence.recordSnapshotFile],['--plan-file',evidence.planFile]])
    assert.ok(f.commands[0].includes("'"+flag+"' '"+path+"'"));
  assert.deepEqual(f.calls,['read','write','read']);
  for(const op of ['check-cas','verify','record-native','import-child-admission'])
    for(const cmd of f.commands.filter(cmd=>cmd.includes("'"+op+"'")))assert.ok(!cmd.includes("'--result-file'"));
  assert.equal(f.commands.filter(cmd=>cmd.includes("'packet-chunk'")).length,0);
  assert.equal(f.commands.filter(cmd=>cmd.includes("'--check-cas-now'")).length,1);
  assert.equal(f.commands.filter(cmd=>cmd.includes("'--import-child-admission'")).length,1);
  assert.equal(f.commands.some(cmd=>cmd.includes("'import-child-admission'")),false);
  assert.equal(f.commands.some(cmd=>cmd.includes("'check-cas'")),false);
  assert.equal(f.commands.some(cmd=>/plan-native|check-native|spawn_agent/.test(cmd)),false);
  assert.equal(f.captures.some(value=>value&&value.message),false);
});
test('Truncated small helper result fails without rerecording or provider operations',async()=>{
  const f=adapterFixture({truncated:'record-native'});
  await assert.rejects(f.adapter.cell.completeAdmission(evidence),/global_local_helper_output_invalid/);
  assert.equal(f.commands.length,1);assert.deepEqual(f.calls,[]);
});
test('Truncated combined verification/import output after write is unknown without reimport',async()=>{
  const f=adapterFixture({truncated:'verify'});
  await assert.rejects(f.adapter.cell.completeAdmission(evidence),/global_cas_outcome_unknown_no_replay/);
  assert.equal(f.commands.filter(cmd=>cmd.includes("'verify'")).length,1);
  assert.equal(f.commands.some(cmd=>cmd.includes("'plan-admitted'")||cmd.includes("'import-child-admission'")),false);
  assert.deepEqual(f.calls,['read','write','read']);
});

test('Combined acceptance/import uses the exact grouped readback and never calls fallback import',async()=>{
  const f=fixture(),verify=f.io.verify;
  let continued;
  f.io.verifyAdmission=async(plan,response,source,context)=>{
    assert.equal(plan.kind,'heartbeat-admitted');continued={source,context};
    const checked=await verify(plan,response,source);
    return {...checked,child_import:{imported:true,route_id:context.routeId,
      native_task_id:context.receipt.native_task_id,admission_receipt_file:context.receipt.admission_receipt_file,
      child_state_dir:'/private/fixed-child'}};
  };
  const result=await f.cell.completeAdmission(evidence);
  assert.equal(result.imported,true);assert.equal(continued.source,f.readbacks[0]);
  assert.equal(continued.context.receipt.native_task_id,'/root/actual-child');
  assert.equal(f.calls.some(c=>c[0]==='import'),false);
  assert.deepEqual(f.calls.filter(c=>c[0]==='write').map(c=>c[1]),['heartbeat-admitted']);
});

for(const [name,childImport] of [['missing import',undefined],['unverified import',{imported:false}],
  ['wrong child',{imported:true,route_id:evidence.routeId,native_task_id:'/root/wrong-child'}]])
  test('Combined acceptance/import '+name+' cannot trigger fallback or replay',async()=>{
    const f=fixture();f.io.verifyAdmission=async()=>({verified:true,first_heartbeat_required:false,child_import:childImport});
    await assert.rejects(f.cell.completeAdmission(evidence),/global_child_import_unverified/);
    assert.equal(f.calls.some(c=>c[0]==='import'),false);
    assert.equal(f.calls.filter(c=>c[0]==='write').length,1);
  });

test('Combined acceptance/import thrown result is uncertain and never imported a second time',async()=>{
  const f=fixture();f.io.verifyAdmission=async()=>{throw Error('lost after import');};
  await assert.rejects(f.cell.completeAdmission(evidence),/global_cas_outcome_unknown_no_replay/);
  assert.equal(f.calls.some(c=>c[0]==='import'),false);
  assert.equal(f.calls.filter(c=>c[0]==='write').length,1);
});

test('JOIN paired acceptance requires an explicit verified first heartbeat',async()=>{
  for(const proof of [{verified:false,first_heartbeat_required:false},{verified:true,first_heartbeat_required:true},
      {verified:true}]){
    const f=fixture();f.io.verify=async()=>proof;
    await assert.rejects(f.cell.joinAndFirstHeartbeat({capacity:2,seconds:90}),/global_first_heartbeat_unverified/);
    assert.equal(f.calls.filter(c=>c[0]==='write').length,1);
    assert.equal(f.calls.some(c=>c[0]==='nextSecond'),false);
  }
});

test('Claim-and-prepare burns native only after exact last accepted CAS/readback, without another provider read',async()=>{
  const f=fixture({due:[true,true]});let preparedSource;
  f.io.planNative=async(source)=>{preparedSource=source;f.calls.push(['planNative',source]);return {
    tool:'collaboration.spawn_agent',arguments:{task_name:'worker'},plan_file:'/private/native-plan',
    record_snapshot_file:'/private/exact-pre-native-snapshot',execute_before:110,checked_at:100};};
  const result=await f.cell.claimAndPrepareNative({routeId:evidence.routeId});
  assert.equal(f.calls.at(-1)[0],'planNative');assert.equal(preparedSource,f.readbacks.at(-1));
  assert.equal(f.sources.length,3);assert.deepEqual(f.calls.filter(c=>c[0]==='write').map(c=>c[1]),
    ['claim-startup','heartbeat']);
  assert.equal(result.claim_verified,true);assert.equal(result.begin_verified,true);
  assert.equal(result.record_snapshot_file,'/private/exact-pre-native-snapshot');
  assert.equal(result.native_spawn_invoked_by_cell,false);assert.equal(result.next_direct_platform_tool_required,true);
});

test('Failed claim-and-prepare verification never reserves native',async()=>{
  const f=fixture();f.io.verify=async()=>{throw Error('unverified claim');};
  f.io.planNative=async()=>{throw Error('must not reserve');};
  await assert.rejects(f.cell.claimAndPrepareNative({routeId:evidence.routeId}),/global_cas_outcome_unknown_no_replay/);
  assert.equal(f.calls.filter(c=>c[0]==='write').length,1);
});
