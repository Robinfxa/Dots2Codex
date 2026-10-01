/* Trusted native functions.exec adapter. No imports, eval, network client, or OAuth.
 * Paste alongside runner.js in the actual admitted native context. This object
 * invokes only the tools supplied by that context. Never run it in Node to reach
 * connectors; Node is used solely for offline tests with fake tools.
 */
function createNativeToolAdapter(tools, config) {
  'use strict';
  const {cwd, root, nativeTaskId} = config;
  if (![cwd,root,nativeTaskId].every(x=>typeof x==='string' && x.length)) throw Error('adapter_config_required');
  const shellQuote = x => "'" + String(x).replace(/'/g,"'\\''") + "'";
  const structured = x => x && Object.prototype.hasOwnProperty.call(x,'structuredContent') ? x.structuredContent : x;
  const captures = new Map(); // handles to exact in-cell responses, never reported
  const manifests = new Map();
  let currentManifest = config.manifest || null;
  const base = ['--root',root,'--native-task-id',nativeTaskId];
  async function command(module,args,input) {
    const argv=['python3','-B','-m',module,...args];
    let cmd=argv.map(shellQuote).join(' ');
    if (input!==undefined) cmd="printf '%s' "+shellQuote(input)+' | '+cmd;
    const r=await tools.exec_command({cmd,workdir:cwd,max_output_tokens:20000,yield_time_ms:10000});
    if(r.session_id || r.exit_code!==0) throw Error('local_helper_failed');
    // Parse only output, not the surrounding exec result object. This avoids
    // escaped-string wrapper amplification and treats JSON null as JSON.
    try{return JSON.parse(r.output);}catch(_){throw Error('local_helper_output_invalid');}
  }
  let packetCounter=0;
  async function savedCommand(module,args) {
    const path=root+'/packet-'+Date.now().toString(36)+'-'+(++packetCounter)+'-'+Math.random().toString(36).slice(2)+'.json';
    await command(module,[...args,'--save',path]);
    let offset=0,sha,parts=[];
    while(true){
      const chunk=await command('remote_transport.connector_files',['packet-chunk','--path',path,
        '--offset',String(offset),'--max-chars','16384',...(sha?['--sha256',sha]:[])]);
      if(chunk.offset_chars!==offset || typeof chunk.text!=='string' || !Number.isInteger(chunk.next_offset_chars) ||
          chunk.next_offset_chars<=offset && !chunk.eof || sha && sha!==chunk.sha256)throw Error('packet_chunk_invalid');
      sha=chunk.sha256;parts.push(chunk.text);offset=chunk.next_offset_chars;
      if(chunk.eof)break;
    }
    try{return JSON.parse(parts.join(''));}catch(_){throw Error('packet_json_invalid');}
  }
  const worker=(op,args=[])=>savedCommand('remote_transport.connector_worker',[op,...base,...args]);
  const batch=(op,args=[])=>savedCommand('remote_transport.connector_batch',[op,...base,...args]);
  async function captureValue(value) {
    const text=JSON.stringify(value);
    if(typeof text!=='string')throw Error('uncapturable_tool_response');
    let result;
    function utf8Bytes(s){let n=0;for(const ch of s){const cp=ch.codePointAt(0);n+=cp<128?1:cp<2048?2:cp<65536?3:4;}return n;}
    if(utf8Bytes(shellQuote(text))<=60000) result=await command('remote_transport.connector_files',['capture','--root',root],text);
    else {
      let state=await command('remote_transport.connector_files',['capture-begin','--root',root]);
      // Fixed bounded chunks avoid shell argv limits even for quote-heavy input.
      // Do not split surrogate pairs; raw JSON text must remain byte-exact UTF-8.
      for(let offset=0;offset<text.length;){
        let end=Math.min(offset+8192,text.length);
        if(end<text.length && /[\uD800-\uDBFF]/.test(text[end-1]))end--;
        state=await command('remote_transport.connector_files',['capture-append','--root',root,
          '--path',state.path,'--offset',String(state.bytes)],text.slice(offset,end));offset=end;
      }
      result=await command('remote_transport.connector_files',['capture-seal','--root',root,'--path',state.path]);
    }
    captures.set(result.path,value);
    return result.path;
  }
  async function captureContext({stage,context,response}) {
    const envelope=await captureValue({stage,context,response});
    const saved=await command('remote_transport.connector_files',['unwrap-capture','--root',root,'--path',envelope]);
    captures.set(saved.path,response);
    return saved.path;
  }
  function captured(path){if(!captures.has(path))throw Error('unknown_capture');return captures.get(path);}
  async function downloadRawResponse({reference,response}) {
    const r=structured(response);
    if(!r || r.id!==reference.locator.file_id || !r.file_uri ||
        typeof r.file_uri.file_id!=='string' || !/^sediment:\/\/file_[A-Za-z0-9_-]+$/.test(r.file_uri.file_id))
      throw Error('exact_raw_file_reference_required');
    return tools.download_file({file_id:r.file_uri.file_id.slice('sediment://'.length)});
  }
  const io={
    startBatch:({seq})=>batch('start',['--seq',String(seq)]),
    upload:({object})=>tools.mcp__codex_apps__google_drive_upload_file({file_uri:object.path,
      file_name:object.name,mime_type:object.mime_type,parent_folder_id:object.folder_id}),
    capture:captureContext,
    recordUpload:({batch_id,object_id,response})=>batch('record-upload',['--batch-id',batch_id,
      '--object-id',object_id,'--response',response]),
    getMetadata:({reference})=>tools.mcp__codex_apps__google_drive_get_file_metadata({fileId:reference.locator.file_id}),
    fetchRaw:({reference,metadata})=>{
      const m=structured(metadata);
      if(!m || m.id!==reference.locator.file_id || typeof m.url!=='string')throw Error('exact_metadata_url_required');
      if(!/^https:\/\/(?:drive|docs)\.google\.com\//.test(m.url))throw Error('untrusted_metadata_url');
      return tools.mcp__codex_apps__google_drive_fetch({url:m.url,download_raw_file:true,include_base64:false});
    },
    materializeRaw:async({reference,response})=>{
      const file=await downloadRawResponse({reference,response:captured(response)});
      await captureContext({stage:'raw_download',context:{reference},response:file});
      if(!file || typeof file.path!=='string')throw Error('download_path_required');
      const saved=await command('remote_transport.connector_files',['materialize','--root',root,'--source',file.path]);
      return saved.path;
    },
    verifyUpload:({batch_id,object_id,metadata,raw_file})=>batch('verify-upload',[
      '--batch-id',batch_id,'--object-id',object_id,'--metadata',metadata,'--raw-file',raw_file]),
    finalize:async({batch_id})=>{
      const result=await batch('finalize',['--batch-id',batch_id,...(currentManifest?['--manifest',currentManifest]:[])]);
      if(result.verified!==true)throw Error('unverified_batch');
      const path=await captureValue(result.entries);currentManifest=path;manifests.set(path,result.entries);
      return {verified:true,manifest:path};
    },
    readControl:()=>tools.mcp__codex_apps__google_drive_get_document({document_id:config.documentId,
      fields:'documentId,revisionId,suggestionsViewMode,tabs'}),
    planCas:({snapshot,evidence})=>worker('tick',['--snapshot',snapshot,
      ...((evidence||currentManifest)?['--manifest',evidence||currentManifest]:[])]),
    reserveWrite:({operation_id})=>batch('reserve-write',['--operation-id',operation_id]),
    casWrite:({tool_arguments})=>tools.mcp__codex_apps__google_drive_batch_update_document(tool_arguments),
    acceptCas:({response,readback})=>worker('accept',['--response',response,'--readback',readback])
  };
  return {io,worker,captureValue,
    // Optional bounded orchestration primitive. Existing materializeRaw keeps
    // its complete capture/materialization path; callers using this primitive
    // must durably capture returned evidence and materialize actual bytes.
    async downloadRaw(input) {
      const file=await downloadRawResponse(input);
      if(!file || typeof file.path!=='string')throw Error('download_path_required');
      return file;
    },
    async now() {
      const result=await tools.exec_command({cmd:"python3 -c 'import time; print(time.monotonic_ns() / 1000000)'",
        workdir:cwd,max_output_tokens:200,yield_time_ms:10000});
      if(result.session_id || result.exit_code!==0)throw Error('monotonic_clock_unavailable');
      const n=Number(result.output.trim());if(!Number.isFinite(n))throw Error('monotonic_clock_unavailable');
      return n;
    },
    async exposeInput({seq,manifest,path}) {
      const packet=await worker('input',['--seq',String(seq),'--manifest',manifest,'--expose-path',path]);
      if(packet.action!=='native_input_file_once')throw Error('file_input_required');
      return packet;
    },
    async inputChunk(packet,offset=0) {
      if(packet.action!=='native_input_file_once')throw Error('file_input_required');
      return command('remote_transport.connector_files',['input-chunk','--path',packet.path,
        '--sha256',packet.sha256,'--offset',String(offset),'--max-chars','2048']);
    }
  };
}
if(typeof module!=='undefined')module.exports={createNativeToolAdapter};
