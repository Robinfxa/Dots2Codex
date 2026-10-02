"""Lossless, request-bound model views with exact on-demand tool definitions.

This module is local and has no admission, exposure, model, network, or execution
side effects. A Worker must first authorize the original input exposure, retain
the original bytes, and record complete schema delivery before accepting calls.
The cache is disposable content storage, never exposure or identity authority.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import json
import re
from pathlib import Path

from .private_io import private_dir, private_lock, read_private_file
from .protocol import ProtocolError, canonical, require, sha256, valid_hash
from .storage import fsync_dir, private_write
from .wire import MAX_BYTES, strict_json, validate_request, validate_response

CONTRACT = 'dots-lite-request-view/1'
SCHEMA_CONTRACT = 'dots-lite-exact-tool/1'
BINDING_KEYS = frozenset({'actor_task_id', 'request_id', 'request_sha256',
                          'package_sha256', 'route_id'})
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_ENTRIES = 16384
MAX_VIEW_BYTES = 8 * 1024 * 1024
MAX_COMPONENT_WORK_BYTES = 64 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024
DISCOVERY_DESCRIPTION_CHARS = 160
_CACHE_NAME = re.compile(r'[0-9a-f]{64}\.json')


def _binding(value, request_hash):
    require(type(value) is dict and set(value) == BINDING_KEYS,
            'invalid_request_view_binding')
    require(all(isinstance(value[key], str) and 0 < len(value[key]) <= 512
                for key in BINDING_KEYS), 'invalid_request_view_binding')
    require(valid_hash(value['package_sha256']) and
            value['request_sha256'] == request_hash, 'request_view_binding_mismatch')
    return copy.deepcopy(value)



def validate_source(raw, *, max_request_bytes=MAX_BYTES):
    """Side-effect-free representability check, for pre-admission validation."""
    require(type(max_request_bytes) is int and 1 <= max_request_bytes <= MAX_BYTES,
            'invalid_request_view_request_limit')
    require(type(raw) is bytes and 0 < len(raw) <= max_request_bytes,
            'request_view_request_too_large')
    request = strict_json(raw)
    validate_request(request, max_bytes=max_request_bytes)
    # The wire parser uses Python floats. Never silently project a decimal
    # constraint that would change under its canonical JSON serialization.
    # Insignificant JSON whitespace/escapes are normalized; authoritative
    # original bytes remain available without any normalization.
    def number_identity(token):
        value = Decimal(token)
        # Numeric equality alone hides a lost minus sign on JSON integer -0.
        return value, value.is_zero() and value.is_signed()
    try:
        original_numbers = json.loads(raw, parse_float=number_identity, parse_int=number_identity)
        projected_numbers = json.loads(canonical(request), parse_float=number_identity, parse_int=number_identity)
    except (ValueError, ArithmeticError):
        raise ProtocolError('request_view_numeric_precision_loss') from None
    require(original_numbers == projected_numbers, 'request_view_numeric_precision_loss')
    return request


def preflight(raw, *, binding, max_request_bytes=MAX_BYTES):
    """Validate the COMPLETE projection privately before BEGIN/exposure burns.

No directory, cache, file, or model-visible artifact is created. Successful
preflight grants no exposure or execution authority; the Worker still gates the
later model view and schema delivery through its original one-use exposure.
    """
    request = validate_source(raw, max_request_bytes=max_request_bytes)
    RequestView(None, raw, request, _binding(binding, sha256(raw)))


def chunk_bytes(data, offset=0, max_bytes=16384):
    """Read a deterministic UTF-8-safe byte range, with locally computed hash.

``complete`` means this range reaches EOF; it does NOT prove earlier ranges were
exposed. The caller must track contiguous delivery from zero for each exact hash.
    """
    require(type(data) is bytes and len(data) <= MAX_VIEW_BYTES,
            'invalid_request_view_bytes')
    require(type(offset) is int and 0 <= offset <= len(data),
            'invalid_request_view_offset')
    require(type(max_bytes) is int and 4 <= max_bytes <= MAX_CHUNK_BYTES,
            'invalid_request_view_chunk_limit')
    try:
        data.decode('utf-8')
        data[:offset].decode('utf-8')
    except UnicodeError:
        raise ProtocolError('invalid_request_view_utf8_boundary') from None
    end = min(len(data), offset + max_bytes)
    while end < len(data) and data[end] & 0xC0 == 0x80:
        end -= 1
    require(end > offset or offset == len(data), 'invalid_request_view_chunk_limit')
    return {'text': data[offset:end].decode('utf-8'), 'offset': offset,
            'next_offset': end, 'total_bytes': len(data), 'sha256': sha256(data),
            'complete': end == len(data)}


class RequestViewStore:
    """Bounded deterministic cache of exact canonical tool/namespace components.

Only locally computed content hashes become filenames. Cache hits are read and
validated byte-for-byte; corruption is an error, never silently repaired. Entries
are evicted in sorted-hash order, independent of wall clocks and model memory.
Construct only after the Worker's exposure gate if even local cache-directory
creation must be delayed until that gate.
    """
    def __init__(self, cache_dir, *, max_bytes=16 * 1024 * 1024,
                 max_entries=4096, max_request_bytes=MAX_BYTES):
        require(type(max_bytes) is int and 1 <= max_bytes <= MAX_CACHE_BYTES,
                'invalid_request_view_cache_limit')
        require(type(max_entries) is int and 1 <= max_entries <= MAX_CACHE_ENTRIES,
                'invalid_request_view_cache_limit')
        require(type(max_request_bytes) is int and 1 <= max_request_bytes <= MAX_BYTES,
                'invalid_request_view_request_limit')
        self.directory = private_dir(cache_dir, create=True)
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self.max_request_bytes = max_request_bytes

    def prepare(self, raw, *, binding):
        request = validate_source(raw, max_request_bytes=self.max_request_bytes)
        bound = _binding(binding, sha256(raw))
        return RequestView(self, raw, request, bound)

    def _component(self, expected):
        """Internal: expected is recomputed from the current validated request."""
        require(type(expected) is bytes and len(expected) <= self.max_bytes,
                'request_view_component_too_large')
        digest = sha256(expected)
        with private_lock(self.directory / 'cache.lock'):
            paths = []
            for path in self.directory.iterdir():
                if path.name == 'cache.lock':
                    continue
                require(_CACHE_NAME.fullmatch(path.name) is not None and
                        not path.is_symlink(), 'request_view_cache_corrupt')
                require(len(paths) < self.max_entries, 'request_view_cache_bounds_exceeded')
                paths.append(path)
            entries, total = [], 0
            for path in sorted(paths, key=lambda item: item.name):
                try:
                    raw = read_private_file(path, self.max_bytes)
                except OSError:
                    raise ProtocolError('request_view_cache_corrupt') from None
                require(sha256(raw) == path.stem, 'request_view_cache_corrupt')
                total += len(raw)
                require(total <= self.max_bytes, 'request_view_cache_bounds_exceeded')
                entries.append((path, raw))
            target = self.directory / (digest + '.json')
            found = next((raw for path, raw in entries if path == target), None)
            if found is not None:
                require(found == expected, 'request_view_cache_corrupt')
                return found
            while entries and (len(entries) >= self.max_entries or
                               sum(len(raw) for _, raw in entries) + len(expected) > self.max_bytes):
                path, _ = entries.pop(0)
                path.unlink()
            private_write(target, expected, immutable=True)
            fsync_dir(self.directory)
            return expected


class RequestView:
    """Immutable source snapshot; returned containers are isolated copies."""
    def __init__(self, store, raw, request, binding):
        self._store, self._raw = store, bytes(raw)
        self._request, self._binding = copy.deepcopy(request), copy.deepcopy(binding)
        self.request_sha256 = sha256(raw)
        self.binding_sha256 = sha256(canonical(binding))
        self._sources, self._references = {}, {}
        self._component_work_bytes = 0
        projected = copy.deepcopy(self._request)

        def project(tool, namespace=None, metadata=None):
            kind, name = tool['type'], tool['name']
            if kind == 'namespace':
                # Keep ALL original namespace metadata and its effective text.
                # Only nested tool definitions are replaced by discovery entries.
                original_metadata = {key: value for key, value in tool.items() if key != 'tools'}
                result = copy.deepcopy(original_metadata)
                result['tools'] = [project(child, name, original_metadata) for child in tool['tools']]
                return result
            key = (namespace, name)
            component = canonical({'definition': tool, 'namespace': namespace,
                                   'namespace_metadata': metadata})
            # Namespace metadata can be large. Bound hashing work and retain
            # source references, not a repeated full metadata blob per child.
            self._component_work_bytes += len(component)
            require(self._component_work_bytes <= MAX_COMPONENT_WORK_BYTES,
                    'request_view_component_work_too_large')
            reference = {'binding_sha256': self.binding_sha256,
                         'request_sha256': self.request_sha256,
                         'schema_sha256': sha256(component), 'namespace': namespace,
                         'name': name, 'type': kind}
            self._sources[key], self._references[key] = (tool, metadata), reference
            result = {'type': kind, 'name': name, 'namespace': namespace,
                      'schema_sha256': reference['schema_sha256']}
            description = tool.get('description')
            if isinstance(description, str):
                result['description_discovery'] = {
                    'text': description[:DISCOVERY_DESCRIPTION_CHARS],
                    'truncated': len(description) > DISCOVERY_DESCRIPTION_CHARS}
            return result

        if isinstance(projected.get('tools'), list):
            projected['tools'] = [project(tool) for tool in self._request['tools']]
        for index, item in enumerate(self._request['input']):
            if item.get('type') == 'additional_tools':
                projected['input'][index]['tools'] = [project(tool) for tool in item['tools']]
        self._model = {'contract': CONTRACT, 'binding': copy.deepcopy(binding),
                       'binding_sha256': self.binding_sha256,
                       'request_sha256': self.request_sha256,
                       'original_byte_length': len(raw),
                       'tool_definition_policy': {
                           'discovery_only': True,
                           'description_discovery': {
                               'kind': 'verbatim_prefix_not_full_constraints',
                               'max_chars': DISCOVERY_DESCRIPTION_CHARS},
                           'exact_current_schema_required_before_call': True,
                           'raw_request_is_authoritative': True,
                           'full_instructions_and_history_preserved': True},
                       'request': projected}
        self._model_raw = canonical(self._model)
        require(len(self._model_raw) <= MAX_VIEW_BYTES, 'request_view_too_large')

    def original_bytes(self):
        return self._raw

    def original_request(self):
        return copy.deepcopy(self._request)

    def model_view(self):
        return copy.deepcopy(self._model)

    def model_view_bytes(self):
        return self._model_raw

    def schema_receipt(self, namespace, name):
        """Current reference, not evidence that a schema was actually delivered."""
        require(namespace is None or isinstance(namespace, str), 'invalid_tool_namespace')
        require(isinstance(name, str), 'invalid_tool_call_name')
        require((namespace, name) in self._references, 'request_view_unadvertised_tool')
        return copy.deepcopy(self._references[(namespace, name)])

    def exact_schema(self, namespace, name, *, reference=None):
        current = self.schema_receipt(namespace, name)
        require(reference is None or reference == current, 'request_view_reference_mismatch')
        definition, metadata = self._sources[(namespace, name)]
        expected = canonical({'definition': definition, 'namespace': namespace,
                              'namespace_metadata': metadata})
        require(sha256(expected) == current['schema_sha256'], 'request_view_source_changed')
        raw = self._store._component(expected)
        # The cache's content has been matched to exact current-request bytes.
        component = strict_json(raw)
        return {'contract': SCHEMA_CONTRACT, 'binding': copy.deepcopy(self._binding),
                'binding_sha256': self.binding_sha256,
                'request_sha256': self.request_sha256,
                'schema_sha256': sha256(raw), 'namespace': component['namespace'],
                'namespace_metadata': component['namespace_metadata'],
                'definition': component['definition'], 'reference': current}

    def validate_exposed(self, response, receipts):
        """Validate calls against authoritative source and trusted delivery ledger.

The Worker owns that ledger. A model-supplied receipt or merely computing a
reference must never be accepted as evidence of full schema exposure.
        """
        validate_response(response, self._request)
        require(type(receipts) is list, 'invalid_request_view_schema_receipts')
        approved = set()
        for receipt in receipts:
            require(type(receipt) is dict, 'invalid_request_view_schema_receipts')
            current = self.schema_receipt(receipt.get('namespace'), receipt.get('name'))
            require(receipt == current, 'request_view_reference_mismatch')
            approved.add((current['namespace'], current['name']))
        for item in response['output']:
            if item.get('type') in {'function_call', 'custom_tool_call'}:
                require((item.get('namespace'), item['name']) in approved,
                        'request_view_schema_not_exposed')
