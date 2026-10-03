"""A bounded stdio adapter built on the installed official Python MCP SDK.

SDK owns JSON-RPC, initialization, tool discovery and cancellation. Thin stream
wrappers enforce byte limits because SDK stdio uses unbounded readline(). The
runtime owns queue authorization, one-use execution and Responses semantics.
No client-supplied _meta field, actor, or binding is used as authority.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import sys
import threading

import anyio
import jsonschema
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from .tools import BRIDGE_TOOLS, GLOBAL_BRIDGE_TOOLS

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_TOOL_RESULT_BYTES = 1536 * 1024
MAX_CONCURRENT_CALLS = 8
MAX_CALL_SECONDS = 10.0


class StreamLimitError(ValueError):
    pass


def redacted_log(event):
    """Only compile-time event labels, never payloads, URLs, IDs or exceptions."""
    allowed = {"stdio_limit", "stdio_invalid_utf8", "stdio_unterminated", "runtime_error",
               "startup_failed", "server_stopped", "server_started", "request_timeout"}
    if event not in allowed:
        event = "runtime_error"
    sys.stderr.write("bridge_mcp: " + event + "\n")
    sys.stderr.flush()


def silence_dependency_logs():
    # SDK validation/debug logging can include complete received messages.
    # This dedicated process intentionally disables those payload-bearing logs.
    logging.disable(logging.CRITICAL)


class BoundedInput:
    """Async text iterator accepted by the SDK's unmodified stdio transport."""
    def __init__(self, raw, max_bytes=MAX_INPUT_BYTES):
        self.raw, self.max_bytes = raw, max_bytes

    def __aiter__(self):
        return self

    async def __anext__(self):
        line = await anyio.to_thread.run_sync(self.raw.readline, self.max_bytes + 1,
                                              abandon_on_cancel=True)
        if not line:
            raise StopAsyncIteration
        if len(line) > self.max_bytes:
            redacted_log("stdio_limit")
            raise StreamLimitError("stdio_limit")
        if not line.endswith(b"\n"):
            redacted_log("stdio_unterminated")
            raise StreamLimitError("stdio_unterminated")
        try:
            return line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            redacted_log("stdio_invalid_utf8")
            raise StreamLimitError("stdio_invalid_utf8") from None


class BoundedOutput:
    def __init__(self, raw, max_bytes=MAX_OUTPUT_BYTES):
        self.raw, self.max_bytes = raw, max_bytes

    async def write(self, text):
        raw = text.encode("utf-8", errors="strict")
        if len(raw) > self.max_bytes:
            redacted_log("stdio_limit")
            raise StreamLimitError("stdio_limit")
        await anyio.to_thread.run_sync(self.raw.write, raw, abandon_on_cancel=True)

    async def flush(self):
        await anyio.to_thread.run_sync(self.raw.flush, abandon_on_cancel=True)


def error_result(code):
    # Codes in this adapter are fixed labels. Runtime exception text is untrusted.
    result = {"error": code}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result))],
                                structuredContent=result, isError=True)


def finite_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class MCPServer:
    """Single-owner runtime bridge. In/out streams are optional binary file objects.

    call_tool must be a synchronous trusted runtime method accepting cancel_event.
    A cancelled/expired call can already have committed an action: clients must
    use the exact action ID and await_result rather than assume rollback.
    """
    def __init__(self, runtime, in_stream=None, out_stream=None, *,
                 max_input_bytes=MAX_INPUT_BYTES, max_output_bytes=MAX_OUTPUT_BYTES,
                 max_concurrent_calls=MAX_CONCURRENT_CALLS, call_timeout=MAX_CALL_SECONDS):
        if type(max_concurrent_calls) is not int or not 1 <= max_concurrent_calls <= 32:
            raise ValueError("invalid_call_limit")
        if not isinstance(call_timeout, (int, float)) or not math.isfinite(call_timeout) or not 0 < call_timeout <= 60:
            raise ValueError("invalid_timeout")
        self.runtime = runtime
        self.in_stream = in_stream if in_stream is not None else sys.stdin.buffer
        self.out_stream = out_stream if out_stream is not None else sys.stdout.buffer
        self.max_input_bytes, self.max_output_bytes = max_input_bytes, max_output_bytes
        self.call_timeout = call_timeout
        self._slots = threading.BoundedSemaphore(max_concurrent_calls)
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent_calls, thread_name_prefix="bridge-mcp")
        self._cancel_events = set()
        self.tool_definitions = GLOBAL_BRIDGE_TOOLS if getattr(runtime, "mode", None) == "global" else BRIDGE_TOOLS
        self._definitions = {item["name"]: item for item in self.tool_definitions}
        self.app = Server("dots2codex-direct-bridge", version="0.3.0",
                          instructions="Single-owner request handoff only. Never execute Mac tools here. Timeouts do not authorize retries with new action IDs.")

        @self.app.list_tools()
        async def list_tools():
            return [types.Tool(**item) for item in self.tool_definitions]

        # Validate locally so exceptions never include argument values in errors.
        @self.app.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            return await self._call_tool(name, arguments)

    async def _call_tool(self, name, arguments):
        definition = self._definitions.get(name)
        if definition is None:
            return error_result("unknown_tool")
        try:
            jsonschema.validate(arguments, definition["inputSchema"])
            if len(finite_json(arguments).encode("utf-8")) > MAX_INPUT_BYTES:
                return error_result("arguments_too_large")
        except (jsonschema.ValidationError, ValueError, TypeError, UnicodeError, RecursionError):
            return error_result("invalid_arguments")
        if not self._slots.acquire(blocking=False):
            return error_result("too_many_inflight_calls")
        cancel_event = threading.Event()
        self._cancel_events.add(cancel_event)

        def invoke():
            try:
                return self.runtime.call_tool(name, arguments, cancel_event=cancel_event)
            finally:
                self._slots.release()

        # shield prevents cancellation of an accepted work item before its finally
        # releases the slot. Runtime sees the separate cooperative cancel event.
        try:
            future = asyncio.get_running_loop().run_in_executor(self._pool, invoke)
        except BaseException:
            self._slots.release()
            self._cancel_events.discard(cancel_event)
            raise
        try:
            with anyio.fail_after(self.call_timeout):
                result = await asyncio.shield(future)
            if type(result) is not dict:
                return error_result("invalid_runtime_result")
            encoded = finite_json(result)
            if len(encoded.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
                return error_result("result_too_large")
            from facade.images import delivery_images
            images = [types.ImageContent(**block) for block in delivery_images(result)]
            # These blocks are the model-visible pixels. Keeping only the JSON
            # in a controller wrapper would discard the actual image channel.
            return types.CallToolResult(content=[types.TextContent(type="text", text=encoded), *images],
                                        structuredContent=result, isError=False)
        except TimeoutError:
            redacted_log("request_timeout")
            return error_result("request_timeout")
        except anyio.get_cancelled_exc_class():
            raise
        except Exception as exc:
            redacted_log("runtime_error")
            code = self.runtime.public_error(exc) if hasattr(self.runtime, "public_error") else "runtime_error"
            return error_result(code)
        finally:
            cancel_event.set()
            self._cancel_events.discard(cancel_event)
            # If cancellation abandons a failing thread, consume its exception to
            # prevent asyncio's default diagnostic from exposing exception text.
            def consume(done):
                if not done.cancelled():
                    done.exception()
            future.add_done_callback(consume)

    async def serve(self):
        silence_dependency_logs()
        redacted_log("server_started")
        try:
            async with stdio_server(
                stdin=BoundedInput(self.in_stream, self.max_input_bytes),
                stdout=BoundedOutput(self.out_stream, self.max_output_bytes),
            ) as (read_stream, write_stream):
                await self.app.run(read_stream, write_stream, self.app.create_initialization_options())
        finally:
            for cancel in list(self._cancel_events):
                cancel.set()
            self._pool.shutdown(wait=False, cancel_futures=False)
            redacted_log("server_stopped")
