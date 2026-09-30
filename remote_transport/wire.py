"""Reuse frozen Responses schema/SSE code; never modify its POSIX store."""
import sys
from pathlib import Path
from .model import ProtocolError, canonical, require
VENDOR = Path(__file__).resolve().parents[1] / 'vendor'
sys.path.insert(0,str(VENDOR))
from core import protocol, QueueError  # verifies frozen source hashes
from facade import ResponsesHandler, ResponsesServer


def validate_text_request(request):
    try:
        unknown = protocol.validate_request(request)
    except protocol.BridgeError as exc:
        raise ProtocolError(exc.code) from None
    require(not unknown, 'unsupported_input')
    require(all(item.get('type','message' if 'role' in item else None) in {'message','additional_tools'}
                for item in request['input']), 'text_only_input_required')
    # Tool advertisements may be present in Codex; no tool call may be returned.
    require(len(canonical(request)) <= 65536, 'wire_request_too_large')
    return request
