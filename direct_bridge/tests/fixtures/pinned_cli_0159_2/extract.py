#!/usr/bin/env python3
"""Extract sanitized contracts from genuine pinned CLI outbound captures.

Usage: python extract.py /path/to/direct-real-contract-capture
No request headers, environment, home paths, bearer tokens, or original
instruction/prompt messages are copied. Exact tool declarations and exact
client callback history are retained without normalization or synthetic repair.
The backend/controller output which induced callbacks was synthetic.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys

CASES = {
    'default': 1, 'web-disabled': 1, 'web-cached': 1, 'web-live': 1,
    'web-indexed': 1, 'web-configured': 1, 'web-text-image': 1,
    'function-roundtrip': 2, 'custom-roundtrip': 2, 'mcp-roundtrip': 2,
    'web-roundtrip': 2, 'web-action-variants': 2, 'reasoning-roundtrip': 2,
    'tool-search-full': 3, 'mcp-image-capable': 2, 'code-mode-enabled': 1,
}
SOURCE_COMMIT = 'ff6aec96948b70d94983af2641a6b67c94faeff5'
BINARY_SHA256 = '1748767b230ebfc3d4ab7e4e254920d0c0ad9691fd8c11f190e7d44511a4a92e'
BASELINE_WIRE_SHA256 = '21679c4637846f20658c076452ce9f7dbf950be415fa04abb66efa89e3896296'

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

def sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()

def extract(root, destination=None):
    root = Path(root)
    destination = Path(destination) if destination else Path(__file__).parent
    output = {}
    for case, last in CASES.items():
        first = json.loads((root/case/'request-1.json').read_text())
        summary = json.loads((root/case/'summary.json').read_text())
        assert summary['client_version'] == 'codex-cli 0.159.2'
        assert summary['returncode'] == 0
        for number in range(1, last + 1):
            path = root/case/f'request-{number}.json'
            raw = json.loads(path.read_text())
            assert raw['input'][:len(first['input'])] == first['input']
            callbacks = copy.deepcopy(raw['input'][len(first['input']):])
            request = {key: copy.deepcopy(raw[key]) for key in
                       ('model', 'reasoning', 'stream', 'tool_choice', 'parallel_tool_calls', 'store', 'include', 'tools')
                       if key in raw}
            request['input'] = [{'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'Sanitized isolated pinned client fixture.'}]}] + callbacks
            value = {
                'contract': 'pinned-codex-client-excerpt/1',
                'case': case, 'request_number': number,
                'evidence': {
                    'actual_client': 'codex-cli 0.159.2',
                    'client_binary_sha256': BINARY_SHA256,
                    'codex_source_commit': SOURCE_COMMIT,
                    'codex_source_url': 'https://github.com/openai/codex/tree/' + SOURCE_COMMIT,
                    'published_direct_baseline_commit': '3510c7270032a2bb67a773f5db53a361bffcb07e',
                    'published_baseline_wire_sha256': BASELINE_WIRE_SHA256,
                    'source_capture': str(path.relative_to(root)),
                    'raw_request_file_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'source_catalog_file_sha256': hashlib.sha256((root/case/'catalog.json').read_bytes()).hexdigest(),
                    'exact_tools_sha256': sha(raw.get('tools', [])),
                    'exact_callback_items_sha256': sha(callbacks),
                    'sanitized_request_sha256': sha(request),
                    'synthetic_server_or_controller_responses': True,
                    'synthetic_local_mcp_data': True,
                    'external_model_api_calls': 0,
                    'live_user_computer_verified': False,
                    'sanitization': 'Initial instructions/environment/user messages replaced; exact tools and all subsequent client history retained. Headers and credentials never captured.',
                    'fixture_only_project_doc_max_bytes': 0,
                },
                'expected_request_error': 'unsupported_input_item' if case == 'reasoning-roundtrip' and number == 2 else None,
                'request': request,
            }
            name = f'{case}-{number}.json'
            (destination/name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            output[name] = hashlib.sha256((destination/name).read_bytes()).hexdigest()
    (destination/'manifest.json').write_text(json.dumps({
        'contract': 'pinned-codex-client-fixture-manifest/1', 'fixture_count': len(output),
        'evidence_boundary': 'Real official pinned CLI outbound requests and callbacks; synthetic response/controller/MCP content. No native-model or live-Mac claim.',
        'files': output}, indent=2) + '\n')
    return output

if __name__ == '__main__':
    print(json.dumps({'fixtures': len(extract(sys.argv[1]))}))
