"""Authenticated, bounded rendezvous, separate from the strict control CAS.

The shared code authenticates protocol data, not a platform/native identity. The
trusted endpoint must obtain the actual identity from its platform. Clearing the
live bundle does not erase provider revision history. No CAS is ever replayed by
this module: an uncertain write is reconciled by its exact signed event only.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass

from .control import _document_text
from .model import Object, canonical, hash_bytes, require, valid_hash
from .selection import validate_selection, validate_admission

BOOT_BEGIN = "DOTS2CODEX_ROUTER_BOOTSTRAP_BEGIN_V2\n"
BOOT_END = "\nDOTS2CODEX_ROUTER_BOOTSTRAP_END_V2\n"
BOOT_CONTRACT = "dots-router-bootstrap/2"
SELECTED_CONTRACT = "dots-router-bootstrap/3"
SELECTED_BEGIN = "DOTS2CODEX_ROUTER_BOOTSTRAP_BEGIN_V3\n"
SELECTED_END = "\nDOTS2CODEX_ROUTER_BOOTSTRAP_END_V3\n"
MAX_BOOT_BYTES = 256 * 1024
STAGES = {"WAITING_FOR_WORKER", "WORKER_ADMITTED", "BUNDLE_READY", "WORKER_POLLING",
          "CONSUMED", "CLOSED", "ABORTED"}
ROOT_KEYS = {"contract", "bootstrap_id", "session_id", "created", "expires",
             "join_code_sha256", "folder_id", "control", "bootstrap_document_id",
             "bootstrap_tab_id", "forward_probe"}
MUTABLE_KEYS = {"stage", "worker", "bundle_hashes", "worker_ack", "bundle_commitment"}
TRANSITIONS = {"worker_admitted": ("WAITING_FOR_WORKER", "WORKER_ADMITTED", "worker"),
               "bundle_ready": ("WORKER_ADMITTED", "BUNDLE_READY", "mac"),
               "worker_polling": ("BUNDLE_READY", "WORKER_POLLING", "worker"),
               "bundle_consumed": ("WORKER_POLLING", "CONSUMED", "mac"),
               "closed": ("CONSUMED", "CLOSED", "mac")}


@dataclass(frozen=True)
class BootstrapSnapshot:
    document_id: str
    tab_id: str
    revision_id: str
    block: str

    @property
    def state(self):
        return decode_block(self.block)


def _safe_id(value, code="invalid_bootstrap_id", max_len=256):
    require(isinstance(value, str) and 1 <= len(value) <= max_len and
            re.fullmatch(r"[A-Za-z0-9_:/.-]+", value), code)
    return value


def _code_bytes(join_code):
    require(isinstance(join_code, str) and 20 <= len(join_code) <= 128 and
            re.fullmatch(r"[A-Za-z0-9_-]+", join_code), "invalid_join_code")
    return join_code.encode("ascii")


def join_code_hash(join_code):
    return hashlib.sha256(_code_bytes(join_code)).hexdigest()


def proof(join_code, purpose, value):
    require(isinstance(purpose, str) and purpose, "invalid_bootstrap_proof_purpose")
    return hmac.new(_code_bytes(join_code), canonical({"purpose": purpose, "value": value},
                    max_bytes=MAX_BOOT_BYTES), hashlib.sha256).hexdigest()


def verify_proof(join_code, purpose, value, expected):
    require(valid_hash(expected) and hmac.compare_digest(proof(join_code, purpose, value), expected),
            "bootstrap_proof_mismatch")


def root_context(state):
    return copy.deepcopy({key: state[key] for key in ROOT_KEYS |
                          ({"required_selection"} if state.get("contract") == SELECTED_CONTRACT else set())})


def context_hash(state):
    return hash_bytes(canonical(root_context(state)))


def _bound(state, value):
    return {"context_hash": context_hash(state), "value": value}


def _now(now):
    value = int(time.time()) if now is None else now
    require(type(value) is int, "invalid_bootstrap_time")
    return value


def check_fresh(state, now=None):
    now = _now(now)
    require(state["created"] <= now < state["expires"] and
            (not state.get("events") or state["events"][-1]["at"] <= now),
            "bootstrap_expired_or_not_yet_valid")
    return now


def b64(raw):
    require(isinstance(raw, bytes), "bootstrap_bytes_required")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def unb64(value):
    require(isinstance(value, str) and len(value) <= MAX_BOOT_BYTES * 2, "invalid_bootstrap_base64")
    try:
        raw = base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)
    except Exception:
        raise ValueError("invalid_bootstrap_base64") from None
    require(len(raw) <= MAX_BOOT_BYTES and b64(raw) == value, "invalid_bootstrap_base64")
    return raw


def forward_probe_bytes(bootstrap_id, nonce):
    return canonical({"contract": "dots-router-forward-probe/1",
                      "bootstrap_id": _safe_id(bootstrap_id, max_len=64),
                      "nonce": _safe_id(nonce, max_len=128)})


def _probe(value, *, forward=False):
    keys = {"file_id", "name", "sha256"} | ({"nonce"} if forward else set())
    require(isinstance(value, dict) and set(value) == keys, "invalid_bootstrap_probe")
    _safe_id(value["file_id"])
    require(isinstance(value["name"], str) and 1 <= len(value["name"]) <= 256 and
            valid_hash(value["sha256"]), "invalid_bootstrap_probe")
    if forward:
        require(isinstance(value["nonce"], str) and 20 <= len(value["nonce"]) <= 128,
                "invalid_bootstrap_probe_nonce")
        _safe_id(value["nonce"], max_len=128)


def _hashes(hashes):
    require(isinstance(hashes, dict) and set(hashes) ==
            {"pin_sha256", "config_sha256", "deployment_hash"} and
            all(valid_hash(v) for v in hashes.values()), "invalid_bootstrap_hashes")


def _logical(state):
    return {"stage": state["stage"], "worker": copy.deepcopy(state["worker"]),
            "bundle_hashes": copy.deepcopy(state["bundle_hashes"]),
            "worker_ack": copy.deepcopy(state["worker_ack"]),
            "bundle_commitment": None if state["bundle"] is None else
                hash_bytes(canonical(state["bundle"], max_bytes=MAX_BOOT_BYTES))}


def _genesis():
    return {"stage": "WAITING_FOR_WORKER", "worker": None, "bundle_hashes": None,
            "worker_ack": None, "bundle_commitment": None}


def _validate_logical(value):
    require(isinstance(value, dict) and set(value) == MUTABLE_KEYS and
            value["stage"] in STAGES, "invalid_bootstrap_logical_state")
    worker, hashes, ack, bundle = (value[k] for k in
                                  ("worker", "bundle_hashes", "worker_ack", "bundle_commitment"))
    if worker is not None:
        require(isinstance(worker, dict) and set(worker) ==
                {"native_task_id", "admitted_at", "probe", "proof"} |
                ({"admission"} if "admission" in worker else set()), "invalid_bootstrap_worker")
        _safe_id(worker["native_task_id"]); _probe(worker["probe"])
        require(type(worker["admitted_at"]) is int and valid_hash(worker["proof"]),
                "invalid_bootstrap_worker")
    if hashes is not None:
        _hashes(hashes)
    if ack is not None:
        require(isinstance(ack, dict) and set(ack) == {"native_task_id", "ready_at", "runtime_hash",
                "bundle_hashes", "forward_probe_sha256", "proof"}, "invalid_bootstrap_worker_ack")
        _safe_id(ack["native_task_id"]); _hashes(ack["bundle_hashes"])
        require(type(ack["ready_at"]) is int and valid_hash(ack["runtime_hash"]) and
                valid_hash(ack["forward_probe_sha256"]) and valid_hash(ack["proof"]),
                "invalid_bootstrap_worker_ack")
        require(worker is not None and ack["native_task_id"] == worker["native_task_id"] and
                ack["bundle_hashes"] == hashes, "bootstrap_worker_ack_binding_mismatch")
    require(bundle is None or valid_hash(bundle), "invalid_bootstrap_bundle_commitment")
    stage = value["stage"]
    expected = {"WAITING_FOR_WORKER": (False, False, False, False),
                "WORKER_ADMITTED": (True, False, False, False),
                "BUNDLE_READY": (True, True, False, True),
                "WORKER_POLLING": (True, True, True, True),
                "CONSUMED": (True, True, True, False), "CLOSED": (True, True, True, False)}
    if stage in expected:
        require(tuple(v is not None for v in (worker, hashes, ack, bundle)) == expected[stage],
                "invalid_bootstrap_stage_state")
    else:
        require(bundle is None, "aborted_bootstrap_bundle_present")


def _validate_transition(before, after, kind, actor):
    _validate_logical(before); _validate_logical(after)
    if kind == "aborted":
        require(before["stage"] not in {"CLOSED", "ABORTED"} and after["stage"] == "ABORTED"
                and actor == "mac", "invalid_bootstrap_transition")
        changed = {"stage", "bundle_commitment"}
    else:
        require(kind in TRANSITIONS and (before["stage"], after["stage"], actor) == TRANSITIONS[kind],
                "invalid_bootstrap_transition")
        changed = {"stage"} | {"worker_admitted": {"worker"},
                 "bundle_ready": {"bundle_hashes", "bundle_commitment"},
                 "worker_polling": {"worker_ack"}, "bundle_consumed": {"bundle_commitment"},
                 "closed": set()}[kind]
    require(all(before[k] == after[k] for k in MUTABLE_KEYS - changed),
            "bootstrap_existing_evidence_changed")


def initial_state(*, bootstrap_id, session_id, created, expires, join_code, folder_id,
                  control_document_id, control_tab_id, control_id, mac_writer_identity,
                  worker_writer_identity, bootstrap_document_id, bootstrap_tab_id, forward_probe, required_selection=None):
    state = {"contract": BOOT_CONTRACT, "bootstrap_id": bootstrap_id, "session_id": session_id,
        "created": created, "expires": expires, "join_code_sha256": join_code_hash(join_code),
        "folder_id": folder_id, "bootstrap_document_id": bootstrap_document_id,
        "bootstrap_tab_id": bootstrap_tab_id, "forward_probe": copy.deepcopy(forward_probe),
        "control": {"document_id": control_document_id, "tab_id": control_tab_id,
                    "control_id": control_id, "mac_writer_identity": mac_writer_identity,
                    "worker_writer_identity": worker_writer_identity},
        "epoch": 0, "stage": "WAITING_FOR_WORKER", "worker": None, "bundle": None,
        "bundle_hashes": None, "worker_ack": None, "events": []}
    if required_selection is not None:
        state["contract"] = SELECTED_CONTRACT
        state["required_selection"] = validate_selection(required_selection)
    state["root_mac"] = proof(join_code, "root", root_context(state))
    validate_state(state)
    return state


def _validate_selected_logical(state, value):
    worker = value['worker']
    if worker is not None:
        if state['contract'] == SELECTED_CONTRACT:
            validate_admission(worker.get('admission'), state['required_selection'], worker['native_task_id'])
        else:
            require('admission' not in worker, 'legacy_bootstrap_cannot_claim_selection')


def validate_state(state):
    require(isinstance(state, dict), 'invalid_bootstrap_state')
    extra = {'required_selection'} if state.get('contract') == SELECTED_CONTRACT else set()
    require(set(state) == ROOT_KEYS | extra | {
            "root_mac", "epoch", "stage", "worker", "bundle", "bundle_hashes", "worker_ack", "events"},
            "invalid_bootstrap_state")
    require(state["contract"] in {BOOT_CONTRACT, SELECTED_CONTRACT}, "invalid_bootstrap_contract")
    if extra: validate_selection(state["required_selection"])
    _safe_id(state["bootstrap_id"], max_len=64)
    for key in ("session_id", "folder_id", "bootstrap_document_id", "bootstrap_tab_id"):
        _safe_id(state[key])
    require(type(state["created"]) is int and type(state["expires"]) is int and
            60 <= state["expires"] - state["created"] <= 7200, "invalid_bootstrap_lifetime")
    require(valid_hash(state["join_code_sha256"]) and valid_hash(state["root_mac"]),
            "invalid_bootstrap_root_proof")
    c = state["control"]
    require(isinstance(c, dict) and set(c) == {"document_id", "tab_id", "control_id",
            "mac_writer_identity", "worker_writer_identity"}, "invalid_bootstrap_control")
    for v in c.values():
        _safe_id(v)
    require(state["bootstrap_document_id"] != c["document_id"], "bootstrap_control_document_must_differ")
    _probe(state["forward_probe"], forward=True)
    fp = state["forward_probe"]
    require(fp["name"] == "dots2codex-router-forward-probe-" + state["bootstrap_id"] + ".json" and
            fp["sha256"] == hash_bytes(forward_probe_bytes(state["bootstrap_id"], fp["nonce"])),
            "bootstrap_forward_probe_mismatch")
    bundle = state["bundle"]
    if bundle is not None:
        require(isinstance(bundle, dict) and set(bundle) == {"pin_b64", "config_b64", "deployment_hash", "mac"}
                and all(valid_hash(bundle[k]) for k in ("deployment_hash", "mac")) and
                all(isinstance(bundle[k], str) for k in ("pin_b64", "config_b64")), "invalid_bootstrap_bundle")
    _validate_logical(_logical(state))
    _validate_selected_logical(state, _logical(state))
    events = state["events"]
    require(type(state["epoch"]) is int and 0 <= state["epoch"] <= 6 and
            type(events) is list and len(events) == state["epoch"], "invalid_bootstrap_events")
    before, parent, operations = _genesis(), context_hash(state), set()
    last_at = state["created"]
    for i, event in enumerate(events, 1):
        require(isinstance(event, dict) and set(event) == {"epoch", "operation_id", "kind", "actor",
                "at", "parent_hash", "before_hash", "after", "reason", "mac"}, "invalid_bootstrap_event")
        require(event["epoch"] == i and isinstance(event["operation_id"], str) and
                re.fullmatch(r"[0-9a-f]{32}", event["operation_id"]) and
                event["operation_id"] not in operations and type(event["at"]) is int and
                event["at"] >= last_at and valid_hash(event["mac"]) and
                event["parent_hash"] == parent and
                event["before_hash"] == hash_bytes(canonical(before)), "invalid_bootstrap_event_chain")
        require(isinstance(event["reason"], str) and len(event["reason"]) <= 256 and
                (event["kind"] == "aborted" or event["reason"] == ""), "invalid_bootstrap_event_reason")
        if event["kind"] not in {"closed", "aborted"}:
            require(event["at"] < state["expires"], "bootstrap_expired_event")
        _validate_transition(before, event["after"], event["kind"], event["actor"])
        _validate_selected_logical(state, event["after"])
        before, parent, last_at = event["after"], hash_bytes(canonical(event)), event["at"]
        operations.add(event["operation_id"])
    require(before == _logical(state), "bootstrap_logical_state_mismatch")
    return state


def verify_join_code(state, join_code):
    validate_state(state)
    require(hmac.compare_digest(state["join_code_sha256"], join_code_hash(join_code)), "join_code_mismatch")
    verify_proof(join_code, "root", root_context(state), state["root_mac"])
    for event in state["events"]:
        verify_proof(join_code, "transition", _bound(state, {k: v for k, v in event.items() if k != "mac"}),
                     event["mac"])
    if state["worker"] is not None:
        value = {k: v for k, v in state["worker"].items() if k != "proof"}
        verify_proof(join_code, "worker_admitted", _bound(state, value), state["worker"]["proof"])
    if state["bundle"] is not None:
        value = {k: v for k, v in state["bundle"].items() if k != "mac"}
        verify_proof(join_code, "bundle", _bound(state, value), state["bundle"]["mac"])
    if state["worker_ack"] is not None:
        value = {k: v for k, v in state["worker_ack"].items() if k != "proof"}
        verify_proof(join_code, "worker_polling", _bound(state, value), state["worker_ack"]["proof"])
        require(value["forward_probe_sha256"] == state["forward_probe"]["sha256"],
                "bootstrap_forward_probe_ack_mismatch")
    return True


def verify_context(state, join_code, document_id, tab_id, *, expected_root=None,
                   now=None, require_fresh=True):
    verify_join_code(state, join_code)
    require(state["bootstrap_document_id"] == document_id and state["bootstrap_tab_id"] == tab_id,
            "bootstrap_source_identity_mismatch")
    if expected_root is not None:
        require(root_context(state) == expected_root, "bootstrap_local_root_mismatch")
    if require_fresh:
        check_fresh(state, now)
    return state


def _event(state, join_code, kind, actor, now, changes, reason=""):
    nxt = copy.deepcopy(state); nxt.update(changes); nxt["epoch"] += 1
    event = {"epoch": nxt["epoch"], "operation_id": secrets.token_hex(16), "kind": kind,
             "actor": actor, "at": now, "parent_hash": hash_bytes(canonical(state["events"][-1]))
             if state["events"] else context_hash(state),
             "before_hash": hash_bytes(canonical(_logical(state))), "after": _logical(nxt), "reason": reason}
    event["mac"] = proof(join_code, "transition", _bound(state, event))
    nxt["events"].append(event); validate_state(nxt)
    return nxt


def block_for(state):
    validate_state(state)
    begin, end = (SELECTED_BEGIN, SELECTED_END) if state["contract"] == SELECTED_CONTRACT else (BOOT_BEGIN, BOOT_END)
    value = begin + canonical(state, max_bytes=MAX_BOOT_BYTES).decode("utf-8") + end
    require(len(value.encode("utf-8")) <= MAX_BOOT_BYTES, "bootstrap_block_too_large")
    return value


def decode_block(block):
    begin, end = (SELECTED_BEGIN, SELECTED_END) if isinstance(block, str) and block.startswith(SELECTED_BEGIN) else (BOOT_BEGIN, BOOT_END)
    require(isinstance(block, str) and len(block.encode("utf-8")) <= MAX_BOOT_BYTES and
            block.startswith(begin) and block.endswith(end) and
            block.count(begin) == 1 and block.count(end) == 1, "invalid_bootstrap_block")
    try:
        state = json.loads(block[len(begin):-len(end)])
    except Exception:
        raise ValueError("invalid_bootstrap_json") from None
    validate_state(state)
    require(block_for(state) == block, "noncanonical_bootstrap_block")
    return state


def snapshot_from_document(document, document_id, tab_id):
    require(isinstance(document, dict) and document.get("documentId") == document_id,
            "bootstrap_document_mismatch")
    revision = document.get("revisionId")
    require(isinstance(revision, str) and revision, "bootstrap_revision_required")
    block = _document_text(document, tab_id); state = decode_block(block)
    require(state["bootstrap_document_id"] == document_id and state["bootstrap_tab_id"] == tab_id,
            "bootstrap_source_identity_mismatch")
    return BootstrapSnapshot(document_id, tab_id, revision, block)


def prepare_update(snapshot, new_state):
    require(isinstance(snapshot, BootstrapSnapshot), "bootstrap_snapshot_required")
    old = snapshot.state; validate_state(new_state)
    require(root_context(new_state) == root_context(old) and new_state["root_mac"] == old["root_mac"] and
            new_state["epoch"] == old["epoch"] + 1 and new_state["events"][:-1] == old["events"],
            "invalid_bootstrap_transition")
    replacement = block_for(new_state)
    request = {"replaceAllText": {"containsText": {"text": snapshot.block[:-1], "matchCase": True,
                "searchByRegex": False}, "replaceText": replacement[:-1], "tabsCriteria": {"tabIds": [snapshot.tab_id]}}}
    return {"document_id": snapshot.document_id, "requests": [request],
            "write_control": {"requiredRevisionId": snapshot.revision_id}}


def _structured(value):
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("structuredContent"), dict):
        return value["structuredContent"]
    if isinstance(value.get("result"), dict) and any(k in value["result"] for k in ("documentId", "revisionId", "tabs")):
        return value["result"]
    return value


def plan(snapshot, new_state, *, join_code):
    verify_context(snapshot.state, join_code, snapshot.document_id, snapshot.tab_id, require_fresh=False)
    verify_join_code(new_state, join_code)
    return {"contract": "dots-router-plan/2", "tab_id": snapshot.tab_id,
            "source_block": snapshot.block, "expected_state": copy.deepcopy(new_state),
            "operation_id": new_state["events"][-1]["operation_id"],
            "tool_arguments": prepare_update(snapshot, new_state)}


def verify_update(plan, response, readback, *, join_code, now=None):
    """Prove one exact transition survived, even if a peer has advanced since it.

    response=None means outcome unknown. It is never permission to repeat a CAS.
    A matching authenticated event is affirmative evidence; absence is unknown.
    """
    require(isinstance(plan, dict) and set(plan) == {"contract", "tab_id", "source_block",
            "expected_state", "operation_id", "tool_arguments"} and
            plan["contract"] == "dots-router-plan/2", "invalid_bootstrap_plan")
    expected, args = plan["expected_state"], plan["tool_arguments"]
    source = BootstrapSnapshot(args["document_id"], plan["tab_id"],
                               args["write_control"]["requiredRevisionId"], plan["source_block"])
    require(plan == globals()["plan"](source, expected, join_code=join_code), "bootstrap_plan_changed")
    response, readback = _structured(response), _structured(readback)
    if response is not None:
        require(isinstance(response, dict) and response.get("documentId") == args["document_id"],
                "bootstrap_response_document_mismatch")
        replies, wc = response.get("replies"), response.get("writeControl")
        require(type(replies) is list and len(replies) == 1 and isinstance(replies[0], dict) and
                isinstance(replies[0].get("replaceAllText"), dict) and
                type(replies[0]["replaceAllText"].get("occurrencesChanged")) is int and
                replies[0]["replaceAllText"]["occurrencesChanged"] == 1, "bootstrap_exact_replace_required")
        require(isinstance(wc, dict) and isinstance(wc.get("requiredRevisionId"), str) and
                wc["requiredRevisionId"] and wc["requiredRevisionId"] != source.revision_id,
                "bootstrap_response_revision_unverified")
    fresh = snapshot_from_document(readback, source.document_id, source.tab_id)
    verify_context(fresh.state, join_code, source.document_id, source.tab_id,
                   expected_root=root_context(expected), now=now,
                   require_fresh=expected["stage"] not in {"CLOSED", "ABORTED"})
    require(fresh.revision_id != source.revision_id and fresh.state["epoch"] >= expected["epoch"] and
            fresh.state["events"][:expected["epoch"]] == expected["events"],
            "bootstrap_operation_not_observed_no_replay")
    return fresh.state


def worker_admitted(state, *, join_code, native_task_id, probe, now=None, admission=None):
    verify_join_code(state, join_code); now = check_fresh(state, now)
    require(state["stage"] == "WAITING_FOR_WORKER", "bootstrap_not_waiting_for_worker")
    _probe(probe)
    value = {"native_task_id": _safe_id(native_task_id), "admitted_at": now, "probe": copy.deepcopy(probe)}
    require(probe["name"] == "dots2codex-router-probe-" + state["bootstrap_id"] + ".json" and
            probe["sha256"] == hash_bytes(canonical({"contract": "dots-router-probe/1",
              "bootstrap_id": state["bootstrap_id"], "native_task_id": native_task_id})),
            "bootstrap_reverse_probe_mismatch")
    if state["contract"] == SELECTED_CONTRACT:
        value["admission"] = validate_admission(admission, state["required_selection"], native_task_id)
    else:
        require(admission is None, "legacy_bootstrap_cannot_claim_selection")
    value["proof"] = proof(join_code, "worker_admitted", _bound(state, value))
    return _event(state, join_code, "worker_admitted", "worker", now,
                  {"worker": value, "stage": "WORKER_ADMITTED"})


def verify_worker_admission(state, join_code, *, now=None):
    verify_join_code(state, join_code); check_fresh(state, now)
    require(state["stage"] in {"WORKER_ADMITTED", "BUNDLE_READY", "WORKER_POLLING", "CONSUMED", "CLOSED"}
            and state["worker"] is not None, "worker_not_admitted")
    return state["worker"]["native_task_id"]


def _validate_bundle_raw(state, pin_raw, config_raw, deployment_hash, now):
    pin = Object.parse(pin_raw)
    require(pin.body["kind"] == "deployment" and pin.oid == deployment_hash and
            pin.body["identity"]["session_id"] == state["session_id"] and
            pin.body["identity"]["native_task_id"] == state["worker"]["native_task_id"],
            "bootstrap_pin_identity_mismatch")
    inference = pin.body['payload'].get('inference')
    if state['contract'] == SELECTED_CONTRACT:
        require(inference == {'selection': state['required_selection'], 'admission': state['worker']['admission']},
                'bootstrap_pin_inference_mismatch')
    else:
        require(inference is None, 'legacy_bootstrap_cannot_claim_selection')
    require(state["created"] <= pin.body["payload"]["created"] <= now < pin.body["payload"]["expires"],
            "bootstrap_pin_expired_or_not_yet_valid")
    try:
        config = json.loads(config_raw)
    except Exception:
        raise ValueError("invalid_worker_config") from None
    c = state["control"]
    require(config == {"document_id": c["document_id"], "tab_id": c["tab_id"],
            "control_id": c["control_id"], "writer_identity": c["worker_writer_identity"],
            "folder_id": state["folder_id"]} and canonical(config) == config_raw,
            "bootstrap_worker_config_mismatch")
    return pin, config


def bundle_ready(state, *, join_code, pin_raw, config_raw, deployment_hash, now=None):
    now = check_fresh(state, now); verify_worker_admission(state, join_code, now=now)
    require(state["stage"] == "WORKER_ADMITTED", "worker_admission_required")
    _validate_bundle_raw(state, pin_raw, config_raw, deployment_hash, now)
    core = {"pin_b64": b64(pin_raw), "config_b64": b64(config_raw), "deployment_hash": deployment_hash}
    core["mac"] = proof(join_code, "bundle", _bound(state, core))
    hashes = {"pin_sha256": hash_bytes(pin_raw), "config_sha256": hash_bytes(config_raw),
              "deployment_hash": deployment_hash}
    return _event(state, join_code, "bundle_ready", "mac", now,
                  {"bundle": core, "bundle_hashes": hashes, "stage": "BUNDLE_READY"})


def extract_bundle(state, *, join_code, native_task_id, now=None):
    verify_join_code(state, join_code); now = check_fresh(state, now)
    require(state["stage"] in {"BUNDLE_READY", "WORKER_POLLING"}, "bootstrap_bundle_not_ready")
    require(state["worker"]["native_task_id"] == native_task_id, "bootstrap_native_identity_mismatch")
    bundle, hashes = state["bundle"], state["bundle_hashes"]
    pin_raw, config_raw = unb64(bundle["pin_b64"]), unb64(bundle["config_b64"])
    require(hash_bytes(pin_raw) == hashes["pin_sha256"] and hash_bytes(config_raw) == hashes["config_sha256"]
            and bundle["deployment_hash"] == hashes["deployment_hash"], "bootstrap_bundle_hash_mismatch")
    _validate_bundle_raw(state, pin_raw, config_raw, bundle["deployment_hash"], now)
    return pin_raw, config_raw


def worker_polling(state, *, join_code, native_task_id, runtime_hash, now=None):
    now = check_fresh(state, now)
    extract_bundle(state, join_code=join_code, native_task_id=native_task_id, now=now)
    require(state["stage"] == "BUNDLE_READY", "bootstrap_worker_already_ready")
    require(valid_hash(runtime_hash), "invalid_worker_runtime_hash")
    ack = {"native_task_id": native_task_id, "ready_at": now, "runtime_hash": runtime_hash,
           "bundle_hashes": copy.deepcopy(state["bundle_hashes"]),
           "forward_probe_sha256": state["forward_probe"]["sha256"]}
    ack["proof"] = proof(join_code, "worker_polling", _bound(state, ack))
    return _event(state, join_code, "worker_polling", "worker", now,
                  {"worker_ack": ack, "stage": "WORKER_POLLING"})


def verify_worker_polling(state, join_code, *, now=None):
    verify_join_code(state, join_code); check_fresh(state, now)
    require(state["stage"] in {"WORKER_POLLING", "CONSUMED", "CLOSED"} and state["worker_ack"],
            "worker_not_polling")
    return state["worker_ack"]


def consume_bundle(state, *, join_code, now=None):
    now = check_fresh(state, now); verify_worker_polling(state, join_code, now=now)
    require(state["stage"] == "WORKER_POLLING", "bootstrap_not_ready_to_consume")
    return _event(state, join_code, "bundle_consumed", "mac", now, {"bundle": None, "stage": "CONSUMED"})


def close_bootstrap(state, *, join_code, now=None):
    verify_join_code(state, join_code)
    require(state["stage"] == "CONSUMED", "bootstrap_not_consumed")
    return _event(state, join_code, "closed", "mac", _now(now), {"stage": "CLOSED"})


def abort_bootstrap(state, *, join_code, reason, now=None):
    verify_join_code(state, join_code)
    require(state["stage"] not in {"CLOSED", "ABORTED"}, "bootstrap_already_final")
    return _event(state, join_code, "aborted", "mac", _now(now),
                  {"stage": "ABORTED", "bundle": None}, str(reason)[:256])
