"""The small static MCP surface; context-dependent native schemas stay lazy."""
from copy import deepcopy

ID = {"type": "string", "minLength": 1, "maxLength": 256}
TOKEN = {"type": "string", "minLength": 1, "maxLength": 256}
WAIT = {"type": "integer", "minimum": 0, "maximum": 5000, "default": 1000}
REQUEST = {"request_id": ID}
CONTEXT = {**REQUEST, "context_token": TOKEN}
ACTION = {**CONTEXT, "action_id": ID, "response": {"type": "object"},
          "schema_tokens": {"type": "array", "items": TOKEN, "maxItems": 128}}


def definition(name, description, properties, required=(), read_only=False):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": deepcopy(properties),
                            "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": read_only, "destructiveHint": not read_only,
                            "openWorldHint": False}}


BRIDGE_TOOLS = [
    definition("bridge_status", "Read bounded bridge health and logical route status.", {}, read_only=True),
    definition("get_request", "Claim or reread the next bound request. A timeout is only an observation; do not create a replacement worker.",
               {"after_seq": {"type": "integer", "minimum": 0, "maximum": 128, "default": 0}, "wait_ms": WAIT}),
    definition("discover_tools", "Find request-bound tool keys and summaries. This does not grant execution or load exact schemas.",
               {**CONTEXT, "query": {"type": "string", "maxLength": 1024},
                "limit": {"type": "integer", "minimum": 1, "maximum": 32, "default": 8}},
               ("request_id", "context_token", "query"), read_only=True),
    definition("lookup_schema", "Read one exact immutable schema bound to this request and context receipt.",
               {**CONTEXT, "name": {"type": "string", "minLength": 1, "maxLength": 1024}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
               ("request_id", "context_token", "name", "sha256"), read_only=True),
    definition("submit_action_and_wait_result", "Commit one native response containing tool intent, then wait for actual Mac output. Retry the same action only; timeout does not permit duplicate execution.",
               {**ACTION, "wait_ms": WAIT}, ("request_id", "action_id", "response", "context_token", "schema_tokens")),
    definition("await_result", "Observe an already submitted action without emitting it again.",
               {**REQUEST, "action_id": ID, "wait_ms": WAIT}, ("request_id", "action_id"), read_only=True),
    definition("finish_request", "Commit the final native response using the acknowledged context. This never executes a Mac tool.",
               ACTION, ("request_id", "action_id", "response", "context_token", "schema_tokens")),
    definition("cancel_request", "Request cooperative cancellation for the bound request; does not undo an emitted side effect.",
               REQUEST, ("request_id",)),
]
