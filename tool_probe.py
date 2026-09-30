"""Experimental one-command function-call proof; never executes the command here."""
import json,re
from portable import QueueError,protocol,digest,encode
COMMAND='python3 -B -c "import secrets; print(\'TOOL_NONCE=\' + secrets.token_hex(12))"'
ARGUMENTS={'cmd':COMMAND,'login':False,'sandbox_permissions':'use_default','yield_time_ms':10000,'max_output_tokens':128}

def validate_function(request,result,jid):
 try:item=protocol.validate_result(result,request,jid)
 except protocol.BridgeError as e:raise QueueError(e.code) from None
 if result.get('kind')!='function_call' or result.get('name')!='exec_command' or encode(result.get('arguments'))!=encode(ARGUMENTS):raise QueueError('tool_probe_intent_not_allowlisted')
 tool=protocol.request_tools(request).get((result.get('namespace'),result['name']))
 schema=tool.get('parameters') if tool else None
 if not isinstance(schema,dict) or schema.get('type')!='object' or len(encode(schema))>65536:raise QueueError('unsupported_advertised_schema')
 def check_refs(v,depth=0):
  if depth>32:raise QueueError('unsupported_advertised_schema')
  if isinstance(v,dict):
   for k,x in v.items():
    if k in ('$ref','$dynamicRef','$recursiveRef'):raise QueueError('schema_reference_rejected')
    check_refs(x,depth+1)
  elif isinstance(v,list):
   for x in v:check_refs(x,depth+1)
 check_refs(schema)
 try:
  from jsonschema import Draft202012Validator
 except ImportError:raise QueueError('tool_schema_validator_unavailable') from None
 try:
  Draft202012Validator.check_schema(schema)
  if not Draft202012Validator(schema).is_valid(result['arguments']):raise QueueError('arguments_do_not_match_advertised_schema')
 except QueueError:raise
 except Exception:raise QueueError('invalid_advertised_schema') from None
 # Schema must actually describe each supplied field; no permissive empty property map.
 props=schema.get('properties',{})
 expected={'cmd':'string','login':'boolean','sandbox_permissions':'string','yield_time_ms':'number','max_output_tokens':'number'}
 for key,typ in expected.items():
  shape=props.get(key)
  if not isinstance(shape,dict) or not isinstance(shape.get('type'),str) or shape.get('type') not in ({'number','integer'} if typ=='number' else {typ}):raise QueueError('unsupported_advertised_schema')
 return item

def output_text(value):
 if isinstance(value,str):return value
 if isinstance(value,list) and value and all(isinstance(p,dict) and p.get('type') in ('input_text','output_text') and isinstance(p.get('text'),str) for p in value):return '\n'.join(p['text'] for p in value)
 raise QueueError('unsupported_tool_output_content')

def expected_final(request,reservation):
 inputs=request.get('input',[])
 calls=[i for i in inputs if i.get('type')=='function_call']
 outputs=[i for i in inputs if i.get('type')=='function_call_output']
 if len(calls)!=1 or len(outputs)!=1 or any(i.get('type') in ('custom_tool_call','custom_tool_call_output') for i in inputs):raise QueueError('tool_probe_history_ambiguous')
 call,out=calls[0],outputs[0]
 if inputs.index(call)>=inputs.index(out):raise QueueError('tool_probe_output_precedes_call')
 if call.get('call_id')!=reservation['call_id'] or out.get('call_id')!=reservation['call_id'] or call.get('name')!='exec_command' or call.get('namespace')!=reservation['namespace']:raise QueueError('tool_probe_call_id_mismatch')
 try:args=protocol.decode(call['arguments'].encode())
 except (KeyError,AttributeError,ValueError,protocol.BridgeError):raise QueueError('tool_probe_call_arguments_mismatch') from None
 if encode(args)!=encode(ARGUMENTS):raise QueueError('tool_probe_call_arguments_mismatch')
 text=output_text(out.get('output'))
 if len(encode(text))>65536:raise QueueError('tool_probe_output_too_large')
 match=re.fullmatch(r"(?:Chunk ID: [A-Za-z0-9_-]+\n)?Wall time: [0-9]+(?:\.[0-9]+)? seconds\nProcess exited with code 0\n(?:Original token count: [0-9]+\n)?(?:Output|Final output):\nTOOL_NONCE=([0-9a-f]{24})\n?",text)
 if match is None:raise QueueError('tool_probe_output_not_successful_nonce')
 matches=[match.group(1)]
 return 'TOOL_OK:'+matches[0][::-1]

def validate_and_reserve(d,s,job,request,result):
 if not isinstance(result,dict):raise QueueError('invalid_result')
 prior=s.get('tool_probe')
 if result.get('kind')=='function_call':
  item=validate_function(request,result,job['id'])
  proposed={'job_id':job['id'],'request_sha256':job['request_sha256'],'call_id':item['call_id'],'namespace':result.get('namespace'),'intent_sha256':digest(result),'command_sha256':digest(COMMAND),'state':'reserved'}
  if prior is not None:
   if prior!=proposed:raise QueueError('tool_probe_single_invocation_limit')
  else:
   # Reserve durably BEFORE output can be emitted. Failure is never a second call.
   s['tool_probe']=proposed;d.save(s)
 elif result.get('kind')=='message':
  if prior is None:raise QueueError('tool_probe_requires_function_call')
  if job['id']==prior['job_id']:raise QueueError('tool_probe_followup_required')
  q=d.queue()
  try:source=q.get(prior['job_id'],owner=d.m['owner_id'],session='session_'+d.m['run_id'])
  finally:q.close()
  if source['state']!='completed' or source['delivery']!='delivered' or digest(source.get('result'))!=prior['intent_sha256']:raise QueueError('tool_probe_source_not_delivered')
  expected=expected_final(request,prior)
  if result!={'kind':'message','text':expected}:raise QueueError('tool_probe_final_must_use_actual_output')
 else:raise QueueError('tool_probe_result_not_supported')
