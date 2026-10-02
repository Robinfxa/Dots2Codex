"""Read local non-secret configuration; never create credentials or access grants."""
import json
import math
import os
from pathlib import Path

REQUIRED = {"db_path", "binding", "client_actor", "worker_actor", "context_epoch",
            "approval_ref", "not_before", "expires_at", "trust_mode"}
OPTIONAL = {"max_wait_ms", "http_wait_ms"}
GLOBAL_REQUIRED = {"mode", "db_path", "config_id", "grant_id", "client_actor", "approval_ref",
                   "not_before", "expires_at", "allowed_pairs", "default_pair", "trust_mode"}
GLOBAL_OPTIONAL = OPTIONAL | {"max_routes", "max_requests_per_route"}


class ConfigError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ConfigError(code)


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate_config_key")
        result[key] = value
    return result


def load_config(path, *, environ=None):
    """Environment input is read at invocation only, not persisted in any file."""
    env = os.environ if environ is None else environ
    with open(path, "rb") as source:
        raw = source.read(65537)
    require(len(raw) <= 65536, "config_too_large")
    def invalid_constant(unused):
        raise ConfigError("invalid_config_number")
    try:
        value = json.loads(raw, object_pairs_hook=strict_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ConfigError("invalid_config") from None
    require(type(value) is dict, "config_object_required")
    global_mode = value.get("mode") == "global"
    required = GLOBAL_REQUIRED if global_mode else REQUIRED
    optional = GLOBAL_OPTIONAL if global_mode else OPTIONAL
    require(required <= set(value), "missing_config_fields")
    require(set(value) <= required | optional, "unknown_or_secret_config_field")
    require(value["trust_mode"] == "single_owner_stdio", "single_owner_stdio_required")
    text_fields = ("client_actor", "config_id", "grant_id", "approval_ref", "db_path") if global_mode else ("client_actor", "worker_actor", "context_epoch", "approval_ref", "db_path")
    for key in text_fields:
        require(type(value[key]) is str and 0 < len(value[key]) <= 4096, "invalid_config_text")
    if not global_mode:
        require(type(value["binding"]) is dict, "binding_object_required")
    else:
        require(type(value["allowed_pairs"]) is list and 1 <= len(value["allowed_pairs"]) <= 32, "invalid_allowed_pairs")
        require(type(value["default_pair"]) is dict and value["default_pair"] in value["allowed_pairs"], "default_pair_not_authorized")
        from facade.global_runtime import catalog_for_pairs
        try:
            catalog_for_pairs(value["allowed_pairs"], value["default_pair"])
        except (ValueError, KeyError, TypeError):
            raise ConfigError("invalid_allowed_pairs") from None
        require(type(value.get("max_routes", 8)) is int and 1 <= value.get("max_routes", 8) <= 32, "invalid_route_capacity")
        require(type(value.get("max_requests_per_route", 128)) is int and 1 <= value.get("max_requests_per_route", 128) <= 1024, "invalid_request_capacity")
    require(not any("REPLACE_" in str(v) for v in value.values()), "replace_example_placeholders")
    for key in ("not_before", "expires_at"):
        if global_mode and key == "expires_at" and value[key] is None:
            continue  # Explicit indefinite local service consent; never extend on restart.
        require(type(value[key]) in (int, float) and math.isfinite(value[key]), "invalid_authorization_window")
    require((global_mode and value["expires_at"] is None) or value["not_before"] < value["expires_at"], "invalid_authorization_window")
    require(type(value.get("max_wait_ms", 5000)) is int and 1 <= value.get("max_wait_ms", 5000) <= 5000,
            "invalid_mcp_wait_limit")
    require(type(value.get("http_wait_ms", 30000)) is int and 1 <= value.get("http_wait_ms", 30000) <= 300000,
            "invalid_http_wait_limit")
    bearer = env.get("DOTS_BRIDGE_HTTP_BEARER")
    require(type(bearer) is str and len(bearer) >= 16 and bearer.isascii()
            and all(33 <= ord(char) <= 126 for char in bearer), "http_bearer_environment_required")
    # Config directory anchors relative state paths. No path is logged.
    db_path = Path(value["db_path"]).expanduser()
    if not db_path.is_absolute():
        db_path = Path(path).resolve().parent / db_path
    value["db_path"] = str(db_path)
    value["http_bearer"] = bearer
    value.pop("trust_mode")
    return value
