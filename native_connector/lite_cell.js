/* dots-lite/3: trusted active-native orchestration. No model API, spawn shim,
 * OAuth client, background loop, heartbeat, or user-input eval. The only eval in
 * the generated loader loads this reviewed, hash-pinned static source. */
'use strict';
function createLiteNativeAdapter(tools, config) {
  if (!config || !['cwd','stateDir','actorTaskId','inboxId'].every(k => typeof config[k] === 'string' && config[k]))
    throw Error('lite_config_required');
  const safeCodes = new Set(['lite_helper_failed','lite_helper_output_invalid','lite_config_required',
    'lite_tool_missing','lite_document_invalid','lite_request_locator_invalid','lite_metadata_invalid',
    'lite_raw_reference_invalid','lite_download_invalid','lite_response_invalid','lite_upload_invalid',
    'lite_write_unknown','lite_source_changed','lite_helper_input_too_large','lite_actor_busy']);
  const captures = new Map(); // Actual responses stay inside this invocation, never text()/console.
  const quote = x => "'" + String(x).replace(/'/g,"'\\''") + "'";
  const unwrap = x => x && Object.prototype.hasOwnProperty.call(x,'structuredContent') ? x.structuredContent : x;
  const own = (x,k) => x && Object.prototype.hasOwnProperty.call(x,k);
  function sanitized(e, category='tool_error') {
    const r=unwrap(e), p=r && typeof r==='object' ? r : {};
    const status=[p.status,p.statusCode,p.status_code].find(n=>Number.isInteger(n)&&n>=100&&n<=599);
    const code=safeCodes.has(e && e.message) ? e.message : 'lite_response_invalid';
    return {category,code,...(status?{status}:{})};
  }
  function requireTool(name) {
    if(typeof tools[name]!=='function') throw Error('lite_tool_missing');
    return tools[name];
  }
  async function captured(name,args) {
    try {
      const value=await requireTool(name)(args);
      captures.set(name,value);
      if(value && value.isError===true) return {isError:true,error:sanitized(value,'provider_error')};
      return value;
    } catch(e) {return {isError:true,error:sanitized(e,'provider_exception')};}
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
    const r=await requireTool('exec_command')({cmd:args.map(quote).join(' '),workdir:config.cwd,
      max_output_tokens:20000,yield_time_ms:10000});
    if(r.session_id || r.exit_code!==0)throw Error('lite_helper_failed');
    let result;try{result=JSON.parse(r.output);}catch(_){throw Error('lite_helper_output_invalid');}
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
  const read = id => captured('mcp__codex_apps__google_drive_get_document',{document_id:id});
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
    const response=await captured('mcp__codex_apps__google_drive_batch_update_document',packet.plan.tool_arguments);
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
      {fileId:d.file_id,fields:'id,mimeType,parents,size,webViewLink,trashed'}),d);
    const response=await captured('mcp__codex_apps__google_drive_fetch',
      {url:m.url,download_raw_file:true,include_base64:false});
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
    const downloaded=await captured('download_file',{file_id:reference.file_id.slice('sediment://'.length)});
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
      file_name:artifact.name,mime_type:'application/json',parent_folder_id:config.folderId});
    const r=unwrap(response);
    // Healthy upload is one call. No metadata/raw self-download or three-object bundle.
    const receipt=!response.isError && r && typeof r.id==='string' && /^[A-Za-z0-9_-]{1,256}$/.test(r.id) ?
      {file_id:r.id,folder_id:config.folderId,byte_length:artifact.byte_length}:null;
    const prepared=await helper('child-publish',{upload_receipt:receipt,
      upload_error:receipt?null:sanitized(response,'upload_unknown')});
    return write('child-accepted',prepared);
  }
  async function reconcile({phase}) {
    if(!['parent-reserved','parent-admitted','child-expose','child-accepted'].includes(phase))throw Error('lite_config_required');
    if(typeof config.outboxId!=='string')throw Error('lite_config_required');
    return helper(phase,{readback:await read(config.outboxId)});
  }
  async function run(action,args={}) {
    try {
      if(action==='parent-prepare')return await parentPrepare();
      if(action==='parent-admit')return await parentAdmit(args);
      if(action==='child-begin')return await childBegin();
      if(action==='child-complete')return await childSave(args);
      if(action==='reconcile')return await reconcile(args);
      if(action==='child-takeover')return await helper('child-takeover',args);
      if(action==='child-retry-upload'){const saved=await helper('child-retry-upload');return saved.ok?await childPublish({artifact:saved.artifact}):saved;}
      if(action==='child-refresh')return await helper('child-refresh',{resource:await read(config.outboxId)});
      if(action==='parent-recover-handoff')return await helper('parent-admitted');
      if(action==='status')return await helper('status');
      throw Error('lite_config_required');
    }catch(e){return {ok:false,error:sanitized(e)};}
  }
  return {run,parentPrepare,parentAdmit,childBegin,childSave,reconcile};
}
if(typeof module!=='undefined')module.exports={createLiteNativeAdapter};
