"""Small support module for reviewed, copied Responses validation/SSE code."""
import hashlib
import json

class ProtocolError(ValueError):
    pass

def require(condition, code):
    if not condition:
        raise ProtocolError(code)

def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError):
        raise ProtocolError('invalid_json') from None

def sha256(raw):
    return hashlib.sha256(raw).hexdigest()
