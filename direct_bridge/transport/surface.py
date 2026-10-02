"""Transport-neutral JSON dispatch. This is NOT an installed MCP connector.

A real HTTPS/MCP adapter must authenticate outside arguments and inject the
pinned Principal/Binding. JsonLoopback models serialization with no sockets.
"""
from dataclasses import asdict
import json
from .queue import Binding, QueueError, canonical, require

NATIVE_TOOLS = ('claim_request', 'read_schema', 'submit_result')
CLIENT_METHODS = ('enqueue_request', 'get_result', 'cancel_request', 'put_schema', 'revoke_route')


class ToolSurface:
    def __init__(self, queue, binding):
        self.queue, self.binding = queue, binding.validate()

    def dispatch(self, principal, name, arguments):
        require(name in NATIVE_TOOLS + CLIENT_METHODS, 'unknown_method')
        require(type(arguments) is dict, 'arguments_required')
        require('principal' not in arguments and 'binding' not in arguments, 'authority_cannot_come_from_wire')
        try:
            return getattr(self.queue, name)(principal, self.binding, **arguments)
        except TypeError:
            raise QueueError('invalid_arguments') from None

    @staticmethod
    def describe_native_tools():
        """MCP-shaped schemas only. Accessibility must be tested in real clients."""
        common = {'request_id': {'type': 'string'}}
        return [
            {'name': 'claim_request', 'description': 'Acquire or reread the same authorized native request; no execution retry.',
             'inputSchema': {'type': 'object', 'properties': common, 'required': ['request_id'], 'additionalProperties': False}},
            {'name': 'read_schema', 'description': 'Read one immutable exact schema referenced by this request.',
             'inputSchema': {'type': 'object', 'properties': {**common, 'name': {'type': 'string'}, 'sha256': {'type': 'string'}},
                             'required': ['request_id', 'name', 'sha256'], 'additionalProperties': False}},
            {'name': 'submit_result', 'description': 'Atomically save an immutable result; identical retries are safe.',
             'inputSchema': {'type': 'object', 'properties': {**common, 'result': {'type': 'object'}},
                             'required': ['request_id', 'result'], 'additionalProperties': False}},
        ]


class JsonLoopback:
    """The trusted principal is a constructor input, never a request field."""
    def __init__(self, surface, principal):
        self.surface, self.principal = surface, principal

    def call(self, method, **arguments):
        wire = json.loads(canonical({'method': method, 'arguments': arguments}))
        result = self.surface.dispatch(self.principal, wire['method'], wire['arguments'])
        return json.loads(canonical(result))
