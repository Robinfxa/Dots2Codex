"""User-invoked foreground entrypoint, stdio MCP plus loopback Responses facade."""
import argparse
import os
import signal
import sys


def main():
    parser = argparse.ArgumentParser(description="Run the single-owner stdio MCP bridge in the foreground.")
    parser.add_argument("--config", required=True, help="Local non-secret route configuration JSON")
    parser.add_argument("--http-port", type=int, default=18765, help="Loopback-only Responses port (default 18765)")
    args = parser.parse_args()
    if not 1 <= args.http_port <= 65535:
        parser.error("http-port must be between 1 and 65535")
    # The tunnel control-plane key is not needed by the MCP child process.
    os.environ.pop("CONTROL_PLANE_API_KEY", None)
    if os.environ.get("DOTS_DIRECT_OWNER_DIR") or os.environ.get("DOTS_DIRECT_RUN_ID"):
        from global_launcher import register_child_owner
        register_child_owner()
    runtime = None
    try:
        import anyio
        from .config import load_config
        from .server import MCPServer, redacted_log, silence_dependency_logs
        from facade.runtime import create_runtime
        silence_dependency_logs()
        config = load_config(args.config)
        runtime = create_runtime(config)
        runtime.start_http(host="127.0.0.1", port=args.http_port)
        server = MCPServer(runtime)
        # SDK specifies EOF/SIGTERM for stdio shutdown. Convert SIGTERM to the
        # same orderly cancellation/close path as Ctrl-C, without a traceback.
        def stop(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        anyio.run(server.serve)
        return 0
    except KeyboardInterrupt:
        return 0
    except ModuleNotFoundError:
        print("bridge_mcp: required_dependency_missing", file=sys.stderr)
        return 2
    except Exception:
        # Never print exception contents: configuration may contain sensitive data.
        print("bridge_mcp: startup_or_runtime_failed", file=sys.stderr)
        return 2
    finally:
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                print("bridge_mcp: shutdown_failed", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
