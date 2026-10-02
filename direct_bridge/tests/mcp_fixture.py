"""Pipe-test fixture ONLY. Never imported by the shipped server entrypoint."""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import anyio
from mcp_adapter.server import MCPServer


class FixtureRuntime:
    def call_tool(self, name, arguments, *, cancel_event=None):
        mode = os.environ.get("MCP_FIXTURE_MODE", "normal")
        if mode == "error":
            raise RuntimeError("private-secret-must-never-leak")
        if mode == "oversize":
            return {"payload": "z" * (1600 * 1024)}
        if mode == "wait" and name == "get_request":
            start = time.monotonic()
            while time.monotonic() - start < 20:
                if cancel_event.wait(0.005):
                    return {"cancelled": True}
        return {"fixture_only": True, "name": name, "arguments": arguments}


async def main():
    server = MCPServer(FixtureRuntime(), max_input_bytes=int(os.getenv("MCP_INPUT_LIMIT", "2097152")),
                       call_timeout=float(os.getenv("MCP_CALL_TIMEOUT", "10")),
                       max_concurrent_calls=int(os.getenv("MCP_CALL_LIMIT", "8")))
    try:
        await server.serve()
    except Exception:
        # Test protocol safety even on malformed transport shutdown.
        print("bridge_mcp: fixture_stopped", file=sys.stderr)


if __name__ == "__main__":
    anyio.run(main)
