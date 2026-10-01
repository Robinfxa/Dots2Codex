'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {createRouterPairingRunner}=require('./router_pairing');

function fixture({bundle=true,failForward=false,unknownAdmit=false}={}) {
  const counts={docs:0,uploads:0,cas:0,prepare:0,planAdmit:0,ready:0,verify:0};
  const events=[];let phase='admit';
  const io={
    context:async()=>{events.push('context');},
    readBootstrap:async()=>{counts.docs++;events.push('bootstrap');return 'bootstrap-'+counts.docs;},
    readControl:async()=>{counts.docs++;events.push('control');return 'control';},
    prepare:async source=>{counts.prepare++;events.push('prepare');return {source};},
    forwardProbe:async()=>{events.push('forward-start');if(failForward)throw Error('forward failed');events.push('forward-end');return 'forward';},
    reverseProbe:async()=>{
      counts.uploads++;events.push('reverse-start');
      await Promise.resolve();await Promise.resolve();events.push('reverse-end');return 'reverse';
    },
    planAdmit:async(source,forward,reverse)=>{
      assert.equal(forward,'forward');assert.equal(reverse,'reverse');
      assert.ok(events.indexOf('reverse-end')>events.indexOf('prepare'));
      counts.planAdmit++;events.push('plan-admit');return {phase:'admit',plan_file:'admit'};
    },
    reserveWrite:async plan=>{events.push('reserve-'+plan.phase);},
    casWrite:async plan=>{
      assert.equal(events.at(-1),'reserve-'+plan.phase);
      phase=plan.phase;counts.cas++;events.push('cas-'+phase);
      if(unknownAdmit&&phase==='admit')throw Error('response lost');return 'response';
    },
    verify:async(plan,response,readback)=>{
      counts.verify++;events.push('verify-'+plan.phase);
      if(unknownAdmit&&plan.phase==='admit')assert.equal(response,null);
      return {verified:true,stage:plan.phase==='ready'?'CONSUMED':bundle?'BUNDLE_READY':'WORKER_ADMITTED',snapshot:readback};
    },
    mayHaveBundle:()=>bundle,
    verifyAndReady:async(plan,response,readback,control)=>{
      const verified=await io.verify(plan,response,readback);
      if(verified.verified!==true)throw Error('unverified admission');
      return {...await io.ready(readback,control),admission_verified:true};
    },
    ready:async(snapshot,control)=>{
      counts.ready++;events.push('ready');
      if(!bundle)return {action:'wait_for_bundle',stage:'WORKER_ADMITTED',retry_writes:false};
      assert.equal(control,'control');return {phase:'ready',plan_file:'ready',worker_runtime:'runtime'};
    },
    captureFailure:async stage=>{events.push(stage);return 'diagnostic';}
  };
  return {io,counts,events,runner:createRouterPairingRunner(io),setBundle:value=>{bundle=value;}};
}

test('immediate bundle path has four Doc gets, two exact CAS readbacks and one upload',async()=>{
  const f=fixture(),result=await f.runner.pairOnce();
  assert.equal(result.action,'paired');assert.equal(result.stage,'CONSUMED');
  assert.deepEqual(f.counts,{docs:4,uploads:1,cas:2,prepare:1,planAdmit:1,ready:1,verify:2});
  assert.equal(f.events.filter(v=>v==='context').length,1);
  assert.ok(f.events.indexOf('plan-admit')>f.events.indexOf('forward-end'));
  assert.ok(f.events.indexOf('plan-admit')>f.events.indexOf('reverse-end'));
});

test('waiting path stops without polling and later readyOnce totals five Doc gets',async()=>{
  const f=fixture({bundle:false});
  const waiting=await f.runner.pairOnce();
  assert.equal(waiting.action,'wait_for_bundle');assert.equal(f.counts.docs,2);
  assert.equal(f.counts.ready,0);assert.equal(f.counts.cas,1);
  f.setBundle(true);const ready=await f.runner.readyOnce();
  assert.equal(ready.action,'paired');assert.equal(f.counts.docs,5);
  assert.equal(f.counts.uploads,1);assert.equal(f.counts.cas,2);
});

test('probe failure drains in-flight upload and emits no admission plan',async()=>{
  const f=fixture({failForward:true});const result=await f.runner.pairOnce();
  assert.equal(result.action,'stop_no_replay');assert.equal(f.events.includes('reverse-end'),true);
  assert.equal(f.counts.uploads,1);assert.equal(f.counts.planAdmit,0);assert.equal(f.counts.cas,0);
});

test('missing actual import produces zero connector reads or probes',async()=>{
  const f=fixture();f.io.context=async()=>{throw Error('missing import');};
  assert.equal((await f.runner.pairOnce()).ok,false);
  assert.equal(f.counts.docs,0);assert.equal(f.counts.uploads,0);assert.equal(f.counts.prepare,0);
});

test('unknown write reconciles exact readback without a second CAS',async()=>{
  const f=fixture({unknownAdmit:true});assert.equal((await f.runner.pairOnce()).ok,true);
  assert.equal(f.counts.cas,2);assert.equal(f.counts.verify,2);
  assert.equal(f.events.includes('cas_outcome_unknown'),true);
});

test('unverified admission readback prevents every readiness action',async()=>{
  const f=fixture();f.io.verify=async()=>{throw Error('missing exact signed event');};
  assert.equal((await f.runner.pairOnce()).ok,false);
  assert.equal(f.counts.cas,1);assert.equal(f.counts.ready,0);
  assert.equal(f.counts.ready,0);
});

test('concurrent runner calls cannot mutate one child ledger in parallel',async()=>{
  const f=fixture();let release;
  f.io.context=()=>new Promise(resolve=>{release=resolve;});
  const first=f.runner.pairOnce();await assert.rejects(f.runner.readyOnce(),/busy/);
  release();assert.equal((await first).ok,true);
});

test('nonthrowing unverified CAS result cannot authorize readiness or completion',async()=>{
  for(const failStage of ['admit','ready']) {
    const f=fixture(),original=f.io.verify;
    f.io.verify=async(plan,response,readback)=>plan.phase===failStage?
      {verified:false,stage:plan.phase==='admit'?'BUNDLE_READY':'CONSUMED',snapshot:readback}:
      original(plan,response,readback);
    const outcome=await f.runner.pairOnce();assert.equal(outcome.ok,false);
    if(failStage==='admit')assert.equal(f.counts.ready,0);
  }
});
