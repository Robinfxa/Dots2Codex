/* dots-lite/3: trusted active-native orchestration. No model API, spawn shim,
 * OAuth client, background loop, heartbeat, or user-input eval. The only eval in
 * the generated loader loads this reviewed, hash-pinned static source. */
'use strict';
function liteOwn(value,key) {
  if(value===null || (typeof value!=='object' && typeof value!=='function'))return undefined;
  const descriptor=Object.getOwnPropertyDescriptor(value,key);
  return descriptor && Object.prototype.hasOwnProperty.call(descriptor,'value')?descriptor.value:undefined;
}
// A functions.exec store/load bridge. Raw captures remain session memory only.
// Across isolates JSON copies values; callCaptured/getCaptures preserve identity
// locally. Unsupported accessors/cycles fail closed rather than invoke or omit.
function createLiteMemoryCaptureSink(store,load,key) {
  if(typeof store!=='function'||typeof load!=='function')throw Error('lite_capture_sink_required');
  function snapshot(value,seen=new Set()) {
    if(value===null || ['string','boolean'].includes(typeof value))return value;
    if(typeof value==='number' && Number.isFinite(value))return value;
    if(value===undefined)return {__lite_capture_type:'undefined'};
    if(typeof value!=='object'||seen.has(value))throw Error('lite_capture_failed');
    const proto=Object.getPrototypeOf(value);
    if(!(value instanceof Error) && proto!==null && proto!==Object.prototype && !(Array.isArray(value)&&proto===Array.prototype))throw Error('lite_capture_failed');
    seen.add(value);
    const descriptors=Object.getOwnPropertyDescriptors(value),array=Array.isArray(value),out=array?[]:Object.create(null);
    if(array){const length=descriptors.length.value,keys=Reflect.ownKeys(descriptors).filter(x=>x!=='length');
      if(keys.length!==length || keys.some(x=>typeof x!=='string'||!/^(0|[1-9][0-9]*)$/.test(x)||Number(x)>=length))throw Error('lite_capture_failed');
    }
    if(value instanceof Error){out.__lite_capture_type='Error';
      for(let proto=value;proto;proto=Object.getPrototypeOf(proto)){
        const name=liteOwn(proto,'name');if(typeof name==='string'){out.name=name;break;}}
    }
    for(const field of Reflect.ownKeys(descriptors)){
      if(Array.isArray(value)&&field==='length')continue;
      const descriptor=descriptors[field];
      if(value instanceof Error && field==='stack' && !Object.prototype.hasOwnProperty.call(descriptor,'value')){
        out.stack={__lite_capture_type:'unevaluated_stack_accessor'};continue;
      }
      if(typeof field!=='string'||!Object.prototype.hasOwnProperty.call(descriptor,'value'))throw Error('lite_capture_failed');
      Object.defineProperty(out,field,{value:snapshot(descriptor.value,seen),enumerable:true,writable:true,configurable:true});
    }
    seen.delete(value);return out;
  }
  return record=>{
    const previous=load(key) || [];
    if(!Array.isArray(previous))throw Error('lite_capture_failed');
    const json=JSON.stringify([...previous.slice(-31),snapshot(record)]);
    if(json.length>4*1024*1024)throw Error('lite_capture_failed');
    store(key,JSON.parse(json));
    if(JSON.stringify(load(key))!==json)throw Error('lite_capture_failed');
    return true;
  };
}
function createLiteNativeAdapter(tools, config) {
  if (!config || !['cwd','stateDir','actorTaskId','inboxId'].every(k => typeof config[k] === 'string' && config[k]))
    throw Error('lite_config_required');
  const safeCodes = new Set(['lite_helper_failed','lite_helper_output_invalid','lite_config_required',
    'lite_tool_missing','lite_document_invalid','lite_request_locator_invalid','lite_metadata_invalid',
    'lite_raw_reference_invalid','lite_download_invalid','lite_response_invalid','lite_upload_invalid',
    'lite_write_unknown','lite_source_changed','lite_helper_input_too_large','lite_actor_busy',
    'lite_capture_failed','lite_capture_sink_required','lite_approval_blocked','lite_transport_unknown']);
  if(typeof config.captureSink!=='function')throw Error('lite_capture_sink_required');
  const captures=[]; // Bounded raw references; never text()/console/command/file output.
  let captureHealthy=true;
  const stages=[];
  const monotonic=typeof performance!=='undefined' && typeof performance.now==='function';
  const clockSource=monotonic?'performance.now':'Date.now';
  const now=monotonic?()=>performance.now():()=>Date.now();
  const elapsed=start=>{const value=now()-start;return Number.isFinite(value)&&value>=0?value:null;};
  const quote = x => "'" + String(x).replace(/'/g,"'\\''") + "'";
  const unwrap = x => x && Object.prototype.hasOwnProperty.call(x,'structuredContent') ? liteOwn(x,'structuredContent') : x;
  const own = (x,k) => x && Object.prototype.hasOwnProperty.call(x,k);
  const approvalCodes=new Set(['approval_denied','approval_required','permission_denied','access_denied',
    'authorization_denied','user_denied','tool_approval_required','insufficient_permissions']);
  const transportCodes=new Set(['ETIMEDOUT','ECONNRESET','ECONNREFUSED','ENETUNREACH','EAI_AGAIN',
    'timeout','transport_error']);
  function sanitized(e, category='provider_unknown') {
    // Only structured allowlisted codes, never message-string guesses or HTTP
    // status alone, distinguish known blockers. Unknowns require raw review.
    const entries=[];let value=e;
    for(let i=0;i<4 && value && typeof value==='object';i++){
      entries.push(value);const error=liteOwn(value,'error');if(error && typeof error==='object')entries.push(error);
      value=liteOwn(value,'structuredContent') || liteOwn(value,'result');
    }
    const status=entries.flatMap(x=>[liteOwn(x,'status'),liteOwn(x,'statusCode'),liteOwn(x,'status_code')]).find(n=>Number.isInteger(n)&&n>=100&&n<=599);
    const codes=entries.map(x=>liteOwn(x,'code')),message=liteOwn(e,'message');
    const code=safeCodes.has(message)?message:
      codes.some(x=>approvalCodes.has(x))?'lite_approval_blocked':
      codes.some(x=>transportCodes.has(x))?'lite_transport_unknown':'lite_response_invalid';
    if(code==='lite_approval_blocked')category='approval_blocked';
    else if(code==='lite_transport_unknown')category='transport_unknown';
    return {category,code,...(status?{status}:{})};
  }
  function requireTool(name) {
    if(typeof tools[name]!=='function') throw Error('lite_tool_missing');
    return tools[name];
  }
  function retain(record) {
    captures.push(record);if(captures.length>32)captures.shift();
    // The injected sink must synchronously acknowledge memory retention before
    // any unwrap, classification, receipt construction or dependent side effect.
    try {if(config.captureSink(record)!==true)captureHealthy=false;}
    catch(_){captureHealthy=false;}
  }
  async function callCaptured(name,args,stage='provider') {
    if(!captureHealthy)throw Error('lite_capture_failed');
    const tool=requireTool(name),start=now();
    let value;
    try {value=await tool(args);}
    catch(error){
      const duration_ms=elapsed(start);
      retain({tool:name,args,kind:'throw',error,stage,duration_ms,clock_source:clockSource});
      stages.push({stage,kind:'provider',outcome:'threw',duration_ms,clock_source:clockSource});
      throw error; // Exact thrown identity; recording is not a tool replacement.
    }
    const duration_ms=elapsed(start);
    retain({tool:name,args,kind:'return',value,stage,duration_ms,clock_source:clockSource});
    stages.push({stage,kind:'provider',outcome:'returned',duration_ms,clock_source:clockSource});
    return value; // Exact result identity, including the original isError flag.
  }
  function providerFailed(value) {
    for(let i=0;i<4 && value && typeof value==='object';i++){
      if(liteOwn(value,'isError')===true || liteOwn(value,'success')===false || liteOwn(value,'error'))return true;
      value=liteOwn(value,'structuredContent') || liteOwn(value,'result');
    }
    return false;
  }
  async function captured(name,args,stage) {
    let value;
    try {value=await callCaptured(name,args,stage);}
    catch(e){
      if(!captureHealthy)throw Error('lite_capture_failed');
      return {isError:true,error:sanitized(e)};
    }
    if(!captureHealthy)throw Error('lite_capture_failed');
    // This safe receipt is separate from the unmodified memory capture.
    return providerFailed(value)?{isError:true,error:sanitized(value)}:value;
  }
  // Input contains only control/metadata/ack data and local paths. The JOIN key
  // stays in private state and plaintext request/result bodies are file-backed.
  // Base64 is quoting, not secrecy; never pass credentials to this function.
  async function helper(operation,payload={}) {
    const input=JSON.stringify(payload);
    if(input.length>196608) throw Error('lite_helper_input_too_large');
    // Portable UTF-8/base64: functions.exec need not expose Node, btoa or TextEncoder.
    const bytes=[];for(const ch of input){const cp=ch.codePointAt(0);
      if(cp<128)bytes.push(cp);else if(cp<2048)bytes.push(192|(cp>>6),128|(cp&63));
      else if(cp<65536)bytes.push(224|(cp>>12),128|((cp>>6)&63),128|(cp&63));
      else bytes.push(240|(cp>>18),128|((cp>>12)&63),128|((cp>>6)&63),128|(cp&63));}
    const alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';let encoded='';
    for(let i=0;i<bytes.length;i+=3){const n=(bytes[i]<<16)|((bytes[i+1]||0)<<8)|(bytes[i+2]||0);
      encoded+=alphabet[(n>>18)&63]+alphabet[(n>>12)&63]+(i+1<bytes.length?alphabet[(n>>6)&63]:'=')+(i+2<bytes.length?alphabet[n&63]:'=');}
    const args=['python3','-B','-m','dots_lite.cli','rpc','--state-dir',config.stateDir,
      '--actor-task-id',config.actorTaskId,...(config.routeId?['--route-id',config.routeId]:[]),'--operation',operation,'--input-base64',encoded];
    if(!captureHealthy)throw Error('lite_capture_failed');
    const tool=requireTool('exec_command'),start=now();
    const timing={stage:operation,kind:'helper',outcome:'pending',duration_ms:null,clock_source:clockSource};stages.push(timing);
    let r;
    try{r=await tool({cmd:args.map(quote).join(' '),workdir:config.cwd,max_output_tokens:20000,yield_time_ms:10000});timing.outcome='returned';}
    catch(error){timing.outcome='threw';throw error;}
    finally{timing.duration_ms=elapsed(start);}
    if(r.session_id || r.exit_code!==0){timing.outcome='error';throw Error('lite_helper_failed');}
    let result;try{result=JSON.parse(r.output);}catch(_){throw Error('lite_helper_output_invalid');}
    if(result && result.local_timing)timing.local=result.local_timing;
    if(!result || result.ok!==true) return result && (result.error || ['unknown','conflicting','upload_unknown'].includes(result.status)) ? result : {ok:false,error:{code:'lite_helper_output_invalid'}};
    return result;
  }
  function plainDoc(response, expectedId) {
    if(!response || response.isError)throw Error('lite_document_invalid');
    let r=unwrap(response); if(r && r.result && !r.documentId)r=r.result;
    if(!r || r.documentId!==expectedId || !Array.isArray(r.tabs) || r.tabs.length!==1)throw Error('lite_document_invalid');
    const t=r.tabs[0], body=t.documentTab ? t.documentTab.body : t.body;
    if(!body || !Array.isArray(body.content))throw Error('lite_document_invalid');
    let text='';for(const block of body.content){
      if(block.sectionBreak)continue;
      if(!block.paragraph || !Array.isArray(block.paragraph.elements))throw Error('lite_document_invalid');
      for(const e of block.paragraph.elements){if(!e.textRun || typeof e.textRun.content!=='string')throw Error('lite_document_invalid');text+=e.textRun.content;}
    }
    if(text.length>32768)throw Error('lite_document_invalid');
    try{return JSON.parse(text.trim());}catch(_){throw Error('lite_document_invalid');}
  }
  function protocolResource(response,kind) {
    // The raw MCP envelope stays solely in memory. Its display/diagnostic content
    // is not a protocol resource. Preserve the COMPLETE unmodified document
    // structure for the Python validator; never strip unsupported body fields.
    const documentFields=new Set(['body','headers','footers','footnotes','documentStyle',
      'suggestedDocumentStyleChanges','namedStyles','suggestedNamedStylesChanges','lists','namedRanges',
      'inlineObjects','positionedObjects','documentId','title','revisionId','suggestionsViewMode',
      'commentsViewMode','tabs','comments','suggestions','document_url','url']);
    const ackFields=new Set(['documentId','replies','writeControl','revisionId']);
    let value=response;
    for(let i=0;i<4 && value && typeof value==='object';i++){
      if(liteOwn(value,'isError')===true)return value===response?response:{isError:true,error:sanitized(value)};
      if(typeof liteOwn(value,'documentId')==='string'){
        const fields=kind==='document'?documentFields:ackFields;
        if(Object.keys(value).some(key=>!fields.has(key)))return {isError:true,error:{category:'provider_unknown',code:'lite_response_invalid'}};
        return {structuredContent:value,isError:false};
      }
      value=liteOwn(value,'structuredContent') || liteOwn(value,'result');
    }
    return {isError:true,error:{category:'provider_unknown',code:'lite_response_invalid'}};
  }
  const read = async id => protocolResource(await captured('mcp__codex_apps__google_drive_get_document',
    {document_id:id},id===config.inboxId?'inbox_read':'outbox_read'),'document');
  async function accept(phase,plan,response) {
    let accepted=await helper(phase,{actual_response:response});
    if(accepted.ok && accepted.status!=='unknown')return accepted;
    if(accepted.status!=='unknown')return accepted;
    const resource=await read(plan.document_id || plan.documentId || plan.tool_arguments.document_id);
    if(resource.isError)return {ok:false,status:'unknown',error:{code:'lite_write_unknown'}};
    return helper(phase,{readback:resource});
  }
  async function write(phase,packet) {
    if(!packet || !packet.ok || !packet.plan || !packet.plan.tool_arguments)return packet;
    const response=protocolResource(await captured('mcp__codex_apps__google_drive_batch_update_document',packet.plan.tool_arguments,phase),'ack');
    return accept(phase,packet.plan,response);
  }
  function locator(inbox) {
    if(!Array.isArray(inbox.routes))throw Error('lite_request_locator_invalid');
    const r=inbox.routes.find(x=>x.route_id===config.routeId), d=r && r.request;
    if(!r || r.outbox_id!==config.outboxId || !d || typeof d.file_id!=='string' ||
        !/^[A-Za-z0-9_-]{1,256}$/.test(d.file_id) || d.folder_id!==config.folderId ||
        !Number.isSafeInteger(d.byte_length) || d.byte_length<1 || d.byte_length>config.maxRequestBytes)
      throw Error('lite_request_locator_invalid');
    return d;
  }
  function metadata(response,d) {
    if(response.isError)throw Error('lite_metadata_invalid');
    const m=unwrap(response);
    const parents=m && (m.parents || m.parent_ids);
    const url=m && (m.url || m.webViewLink);
    // A URL returned by metadata is required. Never construct a fetch URL, accept
    // rendered text, or follow URL query instructions from document content.
    if(!m || m.id!==d.file_id || !Array.isArray(parents) || parents.length!==1 || parents[0]!==config.folderId ||
        typeof url!=='string' || !/^https:\/\/drive\.google\.com\/file\/d\/[A-Za-z0-9_-]+\/(?:view)?(?:\?[^#]*)?$/.test(url) ||
        url.split('/')[5]!==d.file_id ||
        ['application/vnd.google-apps.document','application/vnd.google-apps.spreadsheet'].includes(m.mimeType || m.mime_type))
      throw Error('lite_metadata_invalid');
    const size=m.size ?? m.file_size_bytes ?? m.byte_length;
    if(size===undefined || Number(size)!==d.byte_length || m.trashed===true)throw Error('lite_metadata_invalid');
    return {id:m.id,parents:[...parents],size:String(size),trashed:m.trashed===true,
      file_id:d.file_id,folder_id:parents[0],byte_length:d.byte_length,url,
      mime_type:m.mimeType || m.mime_type || 'application/json'};
  }
  async function materialize(d) {
    const m=metadata(await captured('mcp__codex_apps__google_drive_get_file_metadata',
      {fileId:d.file_id,fields:'id,mimeType,parents,size,webViewLink,trashed'},'input_metadata'),d);
    const response=await captured('mcp__codex_apps__google_drive_fetch',
      {url:m.url,download_raw_file:true,include_base64:false},'input_fetch');
    const r=unwrap(response), reference=r && r.file_uri;
    function noInline(value){
      if(!value || typeof value!=='object')return true;
      for(const key of ['base64','b64_string','text','content'])
        if(own(value,key) && value[key]!==null && value[key]!=='')return false;
      return !value.structuredContent || noInline(value.structuredContent);
    }
    if(response.isError || !r || r.id!==d.file_id || !reference ||
        typeof reference.file_id!=='string' || !/^sediment:\/\/file_[A-Za-z0-9_-]+$/.test(reference.file_id) ||
        !noInline(r))throw Error('lite_raw_reference_invalid');
    const downloaded=await captured('download_file',{file_id:reference.file_id.slice('sediment://'.length)},'input_download');
    const v=unwrap(downloaded);
    if(downloaded.isError || !v || typeof v.path!=='string' || !v.path.startsWith('/') ||
        (v.size_bytes!==undefined && v.size_bytes!==d.byte_length))throw Error('lite_download_invalid');
    // Only path and provider metadata enter helper; no plaintext is emitted here.
    const {url:ignoredProviderUrl,...boundedMetadata}=m;
    return {metadata:boundedMetadata,raw_path:v.path,materialization:{file_id:reference.file_id,path:v.path,
      ...(v.size_bytes!==undefined?{size_bytes:v.size_bytes}:{})}};
  }
  async function parentPrepare() {
    const inbox=await read(config.inboxId);
    const inspected=await helper('parent-inspect',{inbox_resource:inbox});
    if(!inspected.ok)return inspected;
    const outbox=await read(inspected.outbox_id);
    const prepared=await helper('parent-prepare',{inbox_resource:inbox,outbox_resource:outbox});
    return write('parent-reserved',prepared);
  }
  async function parentAdmit({actualArgumentsFile,nativeResultFile}) {
    const prepared=await helper('parent-admit',{actual_arguments_file:actualArgumentsFile,native_result_file:nativeResultFile});
    return write('parent-admitted',prepared);
  }
  async function childBegin() {
    const inbox=await read(config.inboxId), d=locator(plainDoc(inbox,config.inboxId));
    const transfer=await materialize(d);
    const prepared=await helper('child-prepare',{inbox_resource:inbox,...transfer});
    return write('child-expose',prepared);
  }
  async function childSave({requestId,outputFile}) {
    const saved=await helper('child-save',{request_id:requestId,output_file:outputFile});
    return saved.ok ? childPublish({artifact:saved.artifact}) : saved;
  }
  async function childPublish({artifact}) {
    if(!artifact || typeof artifact.path!=='string' || artifact.folder_id!==config.folderId)throw Error('lite_upload_invalid');
    const response=await captured('mcp__codex_apps__google_drive_upload_file',{file_uri:artifact.path,
      file_name:artifact.name,mime_type:'application/json',parent_folder_id:config.folderId},'result_upload');
    const r=unwrap(response);
    // Healthy upload is one call. No metadata/raw self-download or three-object bundle.
    const receipt=!response.isError && r && typeof r.id==='string' && /^[A-Za-z0-9_-]{1,256}$/.test(r.id) ?
      {file_id:r.id,folder_id:config.folderId,byte_length:artifact.byte_length}:null;
    const prepared=await helper('child-publish',{upload_receipt:receipt,
      upload_error:receipt?null:(response.isError?response.error:sanitized(response))});
    return write('child-accepted',prepared);
  }
  async function reconcile({phase}) {
    if(!['parent-reserved','parent-admitted','child-expose','child-accepted'].includes(phase))throw Error('lite_config_required');
    if(typeof config.outboxId!=='string')throw Error('lite_config_required');
    return helper(phase,{readback:await read(config.outboxId)});
  }
  async function dispatch(action,args={}) {
    try {
      if(action==='parent-prepare')return await parentPrepare();
      if(action==='parent-admit')return await parentAdmit(args);
      if(action==='child-begin')return await childBegin();
      if(action==='child-complete')return await childSave(args);
      if(action==='reconcile')return await reconcile(args);
      if(action==='child-takeover')return await helper('child-takeover',args);
      if(action==='child-retry-upload'){const saved=await helper('child-retry-upload',{retry_decision:args.retryDecision});return saved.ok?await childPublish({artifact:saved.artifact}):saved;}
      if(action==='child-result-retry-status')return await helper('child-result-retry-status');
      if(action==='child-retry-result')return await write('child-accepted',await helper('child-retry-result',{
        retry_decision:args.retryDecision,expected_operation_id:args.expectedOperationId,expected_attempt:args.expectedAttempt}));
      if(action==='child-refresh')return await helper('child-refresh',{resource:await read(config.outboxId)});
      if(action==='parent-recover-handoff')return await helper('parent-admitted');
      if(action==='status')return await helper('status');
      throw Error('lite_config_required');
    }catch(e){return {ok:false,error:sanitized(e)};}
  }
  async function run(action,args={}) {
    const start=now(),offset=stages.length;
    const outcome=await dispatch(action,args),measured=stages.slice(offset);
    const safeAction=new Set(['parent-prepare','parent-admit','child-begin','child-complete','reconcile','child-takeover','child-retry-upload','child-result-retry-status','child-retry-result','child-refresh','parent-recover-handoff','status']).has(action)?action:'unknown';
    return {...outcome,diagnostics:{action:safeAction,clock_source:clockSource,duration_ms:elapsed(start),
      provider_calls:measured.filter(x=>x.kind==='provider').length,
      helper_calls:measured.filter(x=>x.kind==='helper').length,stages:measured,
      request_file_read_ms:null,native_inference_ms:null}};
  }
  return {run,parentPrepare,parentAdmit,childBegin,childSave,reconcile,callCaptured,
    getCaptures:()=>captures.slice(),captureHealthy:()=>captureHealthy};
}
if(typeof module!=='undefined')module.exports={createLiteNativeAdapter,createLiteMemoryCaptureSink};
