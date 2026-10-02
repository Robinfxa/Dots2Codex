"""Offline exact context projection. No network, model, or tool execution.

An authenticated adapter supplies identities, not fields taken from user/tool
text. A local delivery cursor proves what the adapter returned, not what a
platform injected or a model retained. See README.md for that boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

CONTRACT = "dots-direct-context/1"
BINDING_KEYS = {"grant_id", "route_id", "session_id", "thread_id", "model", "reasoning_effort"}


class ContextError(ValueError):
    pass


class NeedsWorkingSet(ContextError):
    def __init__(self, total_bytes, limit):
        self.total_bytes, self.limit = total_bytes, limit
        super().__init__(f"exact_payload_exceeds_inline_limit:{total_bytes}>{limit}")


def require(condition, code):
    if not condition:
        raise ContextError(code)


def canonical(value):
    """Structural JSON identity; whitespace/key ordering are not significant."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_loads(raw):
    """Reject duplicate keys and numeric loss before canonical projection."""
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate_json_key")
            value[key] = item
        return value

    def number(token):
        value = Decimal(token)
        return value, value.is_zero() and value.is_signed()

    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        exact = json.loads(raw, parse_int=number, parse_float=number)
        projected = json.loads(canonical(value), parse_int=number, parse_float=number)
        require(exact == projected, "numeric_precision_loss")
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ContextError):
            raise
        raise ContextError("invalid_json") from None


def tool_key(namespace, name):
    return json.dumps([namespace, name], ensure_ascii=False, separators=(",", ":"))


def catalog(request):
    """Keep the complete definition and complete namespace metadata by ref."""
    result = {}

    def visit(tools, namespace=None, metadata=None):
        require(type(tools) is list, "tools_must_be_list")
        for tool in tools:
            require(type(tool) is dict, "invalid_tool")
            name, kind = tool.get("name"), tool.get("type")
            require(isinstance(name, str) and name, "invalid_tool_name")
            if kind == "namespace":
                require(namespace is None, "nested_namespace_unsupported")
                visit(tool.get("tools"), name,
                      {key: val for key, val in tool.items() if key != "tools"})
                continue
            require(kind in {"function", "custom"}, "unsupported_tool_type")
            key = tool_key(namespace, name)
            require(key not in result, "duplicate_tool")
            component = {"namespace": namespace, "namespace_metadata": metadata,
                         "definition": tool}
            result[key] = {"sha256": digest(component), "component": copy.deepcopy(component)}

    visit(request.get("tools", []))
    for item in request["input"]:
        if item.get("type") == "additional_tools":
            visit(item.get("tools"))
    return result


def validate_history(items):
    """Never create/remap a call ID; callbacks must refer to preceding calls."""
    require(type(items) is list, "history_must_be_list")
    calls, outputs = {}, set()
    for item in items:
        require(type(item) is dict, "invalid_history_item")
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call"}:
            call_id = item.get("call_id")
            require(isinstance(call_id, str) and call_id and call_id not in calls,
                    "duplicate_or_missing_call_id")
            calls[call_id] = kind
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = item.get("call_id")
            require(call_id in calls and call_id not in outputs,
                    "unknown_or_duplicate_tool_output")
            require(calls[call_id] + "_output" == kind, "tool_output_kind_mismatch")
            outputs.add(call_id)


@dataclass(frozen=True)
class NativeContinuity:
    actor_id: str
    context_epoch: str

    def __post_init__(self):
        require(bool(self.actor_id) and bool(self.context_epoch), "unknown_native_continuity")


@dataclass(frozen=True)
class Delivery:
    token: str
    revision: dict
    continuity: NativeContinuity
    payload_bytes: bytes

    def tool_result(self, max_bytes=256 * 1024):
        """One complete tool return or an explicit size error. Never truncate."""
        if len(self.payload_bytes) > max_bytes:
            raise NeedsWorkingSet(len(self.payload_bytes), max_bytes)
        return strict_loads(self.payload_bytes)


class ContextStore:
    """One source conversation and one native child, entirely in memory.

    Keep the instance inside the trusted adapter. A new process/instance has no
    delivery cursor: it must receive a full source snapshot and bootstrap again.
    Persistence of source snapshots is safe; blindly persisting a delivery cursor
    over a native restart/compaction is not. No source payload grants authority.
    """
    def __init__(self, binding, *, source_actor):
        require(type(binding) is dict and set(binding) == BINDING_KEYS, "invalid_binding")
        require(all(isinstance(v, str) and v for v in binding.values()), "invalid_binding")
        require(isinstance(source_actor, str) and source_actor, "invalid_source_actor")
        self.binding = copy.deepcopy(binding)
        self.source_actor = source_actor
        self._request = None
        self._revision = None
        self._catalog = {}
        self._pending_output = []
        self._output_recorded = False
        self._last_delta = None
        self._delivery = None
        self._acknowledged = None
        self._delivered_snapshot = None
        self._schema_delivery = {}

    @property
    def revision(self):
        return copy.deepcopy(self._revision)

    def snapshot(self):
        return copy.deepcopy(self._request)

    def schema_refs(self):
        return {key: value["sha256"] for key, value in self._catalog.items()}

    def _authorize(self, actor, binding):
        require(actor == self.source_actor, "untrusted_context_source")
        require(binding == self.binding, "binding_mismatch")

    def _accept(self, revision, request):
        require(type(revision) is int and revision > 0, "invalid_revision")
        # Round-trip prevents callers from supplying non-JSON Python containers.
        candidate = strict_loads(canonical(request))
        require(type(candidate) is dict and type(candidate.get("input")) is list,
                "invalid_request")
        validate_history(candidate["input"])
        schemas = catalog(candidate)
        ref = {"revision": revision, "sha256": digest(candidate)}
        if self._revision == ref:
            return self.revision  # Exact transport retry; no second delivery/execution.
        require(self._delivery is None, "previous_delivery_unacknowledged")
        if self._revision:
            require(revision == self._revision["revision"] + 1, "revision_not_next")
            expected = self._request["input"] + self._pending_output
            require(canonical(candidate["input"][:len(expected)]) == canonical(expected),
                    "history_prefix_changed")
        else:
            require(revision == 1, "initial_revision_must_be_one")
        self._request, self._revision, self._catalog = candidate, ref, schemas
        self._pending_output = []
        self._output_recorded = False
        self._last_delta = None
        return self.revision

    def ingest_full(self, *, actor, binding, revision, request):
        self._authorize(actor, binding)
        return self._accept(revision, request)

    def ingest_delta(self, *, actor, binding, base, revision, append,
                     set_fields=None, delete_fields=(), tools=None):
        self._authorize(actor, binding)
        require(self._request is not None, "full_snapshot_required")
        fingerprint = digest({"base": base, "revision": revision, "append": append,
                              "set_fields": set_fields, "delete_fields": list(delete_fields), "tools": tools})
        if self._last_delta == fingerprint:
            return self.revision
        require(canonical(base) == canonical(self._revision), "base_reference_mismatch")
        require(type(append) is list, "append_must_be_list")
        updates = set_fields or {}
        require(type(updates) is dict and type(delete_fields) in {list, tuple},
                "invalid_field_changes")
        require(all(isinstance(key, str) for key in delete_fields), "invalid_deleted_field")
        require(not ({"input", "tools"} & (set(updates) | set(delete_fields))),
                "history_or_tools_override_forbidden")
        require(not (set(updates) & set(delete_fields)), "conflicting_field_changes")
        candidate = copy.deepcopy(self._request)
        candidate["input"].extend(copy.deepcopy(append))
        for key in delete_fields:
            require(key in candidate, "deleted_field_absent")
            del candidate[key]
        candidate.update(copy.deepcopy(updates))
        if tools is not None:
            candidate["tools"] = copy.deepcopy(tools)
        accepted = self._accept(revision, candidate)
        self._last_delta = fingerprint
        return accepted

    @staticmethod
    def _project_history(items):
        projected = copy.deepcopy(items)
        for item in projected:
            if item.get("type") == "additional_tools":
                # Only tool declarations may move behind exact schema lookup.
                # All other history text/metadata stays byte-for-byte as JSON values.
                declarations = item.pop("tools")
                require("exact_tool_declarations_ref" not in item, "reserved_projection_field")
                item["exact_tool_declarations_ref"] = digest(declarations)
        return projected

    def prepare_delivery(self, continuity, *, rehydrate_reason=None):
        require(type(continuity) is NativeContinuity, "unknown_native_continuity")
        require(self._request is not None, "full_snapshot_required")
        acknowledged = self._acknowledged
        continuous = (acknowledged is not None and acknowledged.continuity == continuity
                      and rehydrate_reason is None)
        if continuous and acknowledged.revision == self._revision:
            raise ContextError("revision_already_delivered")
        if self._delivery is not None and self._delivery.revision == self._revision:
            require(self._delivery.continuity == continuity and not rehydrate_reason,
                    "pending_delivery_continuity_changed")
            return self._delivery
        fields = {key: val for key, val in self._request.items() if key not in {"input", "tools"}}
        payload = {"contract": CONTRACT, "binding": copy.deepcopy(self.binding),
                   "revision": self.revision,
                   "native": {"actor_id": continuity.actor_id, "context_epoch": continuity.context_epoch},
                   "schema_policy": "exact_current_schema_delivery_required_before_call",
                   "tool_catalog": {"count": len(self._catalog), "discovery": "on_demand"}}
        if not continuous:
            payload.update(kind="full", base_revision=None,
                           rehydrate_reason=rehydrate_reason or "no_acknowledged_same_actor_epoch",
                           request_fields=fields, history=self._project_history(self._request["input"]))
        else:
            old = self._delivered_snapshot
            old_fields = {key: val for key, val in old.items() if key not in {"input", "tools"}}
            old_catalog = catalog(old)
            payload.update(kind="delta", base_revision=copy.deepcopy(acknowledged.revision),
                           set_fields={key: val for key, val in fields.items()
                                       if key not in old_fields or canonical(old_fields[key]) != canonical(val)},
                           delete_fields=sorted(set(old_fields) - set(fields)),
                           append=self._project_history(self._request["input"][len(old["input"]):]))
            payload["tool_catalog"].update(
                changed={key: value["sha256"] for key, value in self._catalog.items()
                         if key not in old_catalog or old_catalog[key]["sha256"] != value["sha256"]},
                removed=sorted(set(old_catalog) - set(self._catalog)))
        raw = canonical(payload)
        delivery = Delivery(hashlib.sha256(raw).hexdigest(), self.revision, continuity, raw)
        self._delivery = delivery
        return delivery

    def acknowledge_delivery(self, delivery):
        """Trusted adapter only, AFTER the complete result was actually returned.

        This is a local emission record, never a model-read or context-injection
        attestation. Lost/unknown return: do not call it. Retry same delivery.
        """
        require(delivery == self._delivery and delivery.revision == self._revision,
                "delivery_not_current")
        if (self._acknowledged is None or self._acknowledged.continuity != delivery.continuity
                or strict_loads(delivery.payload_bytes)["kind"] == "full"):
            self._schema_delivery.clear()
        self._acknowledged, self._delivered_snapshot = delivery, self.snapshot()
        self._delivery = None

    def record_output(self, *, continuity, items):
        """Record exact committed model output; never execute its tool calls."""
        require(self._acknowledged is not None
                and self._acknowledged.continuity == continuity
                and self._acknowledged.revision == self._revision,
                "output_without_current_delivery")
        require(type(items) is list, "invalid_output")
        require(not self._output_recorded or canonical(self._pending_output) == canonical(items),
                "output_already_committed")
        validate_history(self._request["input"] + items)
        self._pending_output = copy.deepcopy(items)
        self._output_recorded = True

    def discover(self, query, *, limit=8):
        require(isinstance(query, str) and query.strip(), "nonempty_discovery_query_required")
        require(type(limit) is int and 1 <= limit <= 32, "invalid_discovery_limit")
        words = query.casefold().split()
        found = []
        for key, value in sorted(self._catalog.items()):
            component = value["component"]
            definition = component["definition"]
            text = " ".join(str(definition.get(k, "")) for k in ("name", "description"))
            text += " " + str(component["namespace"] or "")
            if all(word in text.casefold() for word in words):
                found.append({"key": key, "namespace": component["namespace"],
                              "name": definition["name"], "type": definition["type"],
                              "schema_sha256": value["sha256"]})
        return {"matches": found[:limit], "more": len(found) > limit,
                "total_matches": len(found), "revision": self.revision}

    def exact_schema(self, key, expected_digest):
        require(key in self._catalog, "unknown_tool")
        value = self._catalog[key]
        require(expected_digest == value["sha256"], "stale_schema_reference")
        return {"key": key, "schema_sha256": expected_digest,
                "component": copy.deepcopy(value["component"])}

    def acknowledge_schema_delivery(self, *, continuity, key, expected_digest):
        require(self._acknowledged is not None and self._acknowledged.continuity == continuity,
                "schema_without_native_continuity")
        self.exact_schema(key, expected_digest)
        self._schema_delivery[(continuity, key)] = expected_digest

    def require_schema(self, *, continuity, key):
        require(key in self._catalog, "unknown_tool")
        require(self._schema_delivery.get((continuity, key)) == self._catalog[key]["sha256"],
                "exact_current_schema_not_delivered")
        return self.exact_schema(key, self._catalog[key]["sha256"])
