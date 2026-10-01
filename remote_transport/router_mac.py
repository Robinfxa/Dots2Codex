"""Bounded Mac pairing supervisor with durable cancellation and exact CAS evidence.

Creates distinct bootstrap/control Docs and verifies both raw-file directions.
Only current bootstrap content is cleared; provider history can retain pin/config.
No credentials, controller journal, prompts or model results enter bootstrap.
Native admission and inference require a separately active, authorized endpoint.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import stat
import http.client
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import Journal, Object, deployment
from .backend import read_private_file, fsync_dir
from .cli import write_new
from .control import (_document_text, GoogleDocsCASControlStore, SessionCoordinator,
                      binding_for, CASConflict)
from .model import canonical, require, hash_bytes
from .operator import codex_command, ready_selection, selection_status
from .selection import load_catalog, select, validate_selection, catalog_hash
from .router_bootstrap import (
    block_for as bootstrap_block_for,
    initial_state as bootstrap_initial_state,
    snapshot_from_document as bootstrap_snapshot_from_document,
    prepare_update as bootstrap_prepare_update,
    verify_update as bootstrap_verify_update,
    verify_worker_admission, bundle_ready, verify_worker_polling, consume_bundle,
    close_bootstrap, abort_bootstrap, plan as bootstrap_plan,
    root_context, verify_context,
)
from .session import _save

DEFAULT_CONFIG = Path.home() / ".config" / "dots2codex" / "router.json"
DEFAULT_ACTIVE = Path.home() / ".config" / "dots2codex" / "router-active.json"
EXPECTED_CODEX = "codex-cli 0.159.2"


def _private_json(path, value):
    path = Path(path); path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _save(path, value)


def _load_private_json(path, limit=2 * 1024 * 1024):
    return json.loads(read_private_file(Path(path), limit))


def _config_path(value):
    return Path(value).expanduser().resolve()


def _set_google_env(config):
    credential = Path(config["authorized_user_file"]).expanduser().resolve()
    require(credential.is_absolute(), "absolute_authorized_user_file_required")
    os.environ["DOTS_GOOGLE_AUTHORIZED_USER_FILE"] = str(credential)
    if not os.environ.get("SSL_CERT_FILE"):
        try:
            import certifi
            os.environ["SSL_CERT_FILE"] = certifi.where()
        except Exception:
            pass


def _validate_config(c):
    required = {"folder_id", "authorized_user_file", "workdir", "mac_writer_identity",
                "worker_writer_identity", "seconds", "max_requests", "scope", "port",
                "deadline", "poll_interval", "heartbeat_interval", "bootstrap_ttl",
                "bootstrap_poll_interval", "codex", "expected_codex_version"}
    optional = {"model_selection", "catalog"}
    require(isinstance(c, dict) and required <= set(c) <= required | optional, "invalid_router_config")
    require(isinstance(c["folder_id"], str) and c["folder_id"], "router_folder_required")
    require(Path(c["authorized_user_file"]).expanduser().is_absolute(), "absolute_authorized_user_file_required")
    require(Path(c["workdir"]).expanduser().is_absolute(), "absolute_workdir_required")
    require(c["scope"] in {"text_only", "responses_tools"}, "invalid_router_scope")
    require(type(c["seconds"]) is int and 60 <= c["seconds"] <= 28800, "invalid_router_seconds")
    require(type(c["max_requests"]) is int and 1 <= c["max_requests"] <= 128, "invalid_router_request_budget")
    require(type(c["port"]) is int and 0 <= c["port"] <= 65535, "invalid_router_port")
    require(type(c["deadline"]) in (int, float) and 60 <= c["deadline"] <= 3600, "invalid_router_deadline")
    require(type(c["bootstrap_ttl"]) is int and 300 <= c["bootstrap_ttl"] <= 7200, "invalid_bootstrap_ttl")
    require(type(c["bootstrap_poll_interval"]) in (int, float) and 2 <= c["bootstrap_poll_interval"] <= 30,
            "invalid_bootstrap_poll_interval")
    if c.get("model_selection") is not None: validate_selection(c["model_selection"])
    if "catalog" in c:
        require(isinstance(c["catalog"], str) and c["catalog"] and Path(c["catalog"]).expanduser().is_absolute(),
                "absolute_catalog_path_required")
    return c


def _load_config(path):
    return _validate_config(_load_private_json(_config_path(path), 131072))


def _resolve_selection(args, config):
    """Choose only before pairing. Partial overrides never inherit another effort."""
    model = getattr(args, "model", None); effort = getattr(args, "effort", None)
    require((model is None) == (effort is None), "model_and_effort_required_together")
    path = getattr(args, "catalog", None) or config.get("catalog")
    existing = config.get("model_selection")
    if model is None and existing is None and path is None: return None
    catalog = load_catalog(_config_path(path) if path is not None else None)
    if model is not None: return select(catalog, model, effort)
    return validate_selection(existing, catalog) if existing is not None else None


def models(args):
    """Offline supported snapshot, not a live account or native inference probe."""
    catalog = load_catalog(_config_path(args.catalog) if args.catalog else None)
    return {"catalog_version": catalog["version"], "catalog_sha256": catalog_hash(catalog),
            "source": catalog["source"], "models": catalog["models"],
            "native_admission_required": True, "live_availability_verified": False,
            "inference_calls": 0, "limits": catalog["limits"]}


def _create_workspace_doc(drive, folder_id, title):
    try:
        return drive.create_document_once(folder_id, title)
    except Exception:
        raise RuntimeError("router_drive_document_create_unknown") from None


def _blank_doc_info(docs, document_id):
    return _blank_resource_info(docs.get_document(document_id), document_id)


def _blank_resource_info(doc, document_id):
    require(isinstance(doc, dict), "router_document_mismatch")
    require(doc.get("documentId") == document_id, "router_document_mismatch")
    tabs = doc.get("tabs")
    require(type(tabs) is list and len(tabs) == 1, "router_single_tab_required")
    tab = tabs[0]
    tab_id = tab.get("tabProperties", {}).get("tabId") if "tabProperties" in tab else tab.get("tabId")
    require(isinstance(tab_id, str) and tab_id, "router_tab_id_required")
    require(_document_text(doc, tab_id) == "\n", "router_document_not_blank")
    return tab_id, doc


def _operation(runtime, name, arguments):
    """Reserve a single external attempt durably before dispatch. Never replay."""
    path = Path(runtime) / (name + ".json")
    record = {"id": uuid.uuid4().hex, "status": "dispatched", "arguments": arguments}
    write_new(path, canonical(record, max_bytes=2 * 1024 * 1024))
    return path, record


def _finish_operation(path, record, **changes):
    record = {**record, **changes}; _private_json(path, record)
    return record


def _initialize_bootstrap(docs, document_id, tab_id, state, runtime, *, source_resource=None):
    # Only an exact full blank resource captured earlier in this same bounded
    # preparation may be supplied. The provider revision still guards the write.
    doc = docs.get_document(document_id) if source_resource is None else source_resource
    actual_tab, doc = _blank_resource_info(doc, document_id)
    require(actual_tab == tab_id, "router_tab_id_mismatch")
    require(_document_text(doc, tab_id) == "\n", "bootstrap_document_must_be_blank")
    revision = doc.get("revisionId"); require(isinstance(revision, str) and revision, "bootstrap_revision_required")
    args = {"document_id": document_id,
            "requests": [{"insertText": {"location": {"index": 1, "tabId": tab_id},
                                         "text": bootstrap_block_for(state)[:-1]}}],
            "write_control": {"requiredRevisionId": revision}}
    path, rec = _operation(runtime, "initialize-bootstrap", args)
    try:
        docs.batch_update_document(args["document_id"], args["requests"], args["write_control"])
    except Exception:
        pass  # Read-only exact reconciliation, never resubmit.
    try:
        fresh = bootstrap_snapshot_from_document(docs.get_document(document_id), document_id, tab_id)
        require(fresh.state == state and fresh.revision_id != revision, "bootstrap_initialization_readback_mismatch")
    except Exception:
        _finish_operation(path, rec, status="unknown")
        raise RuntimeError("bootstrap_initialization_outcome_unknown") from None
    _finish_operation(path, rec, status="verified", revision=fresh.revision_id)
    return fresh


def _bootstrap_cas(docs, snapshot, new_state, *, join_code, runtime):
    value = bootstrap_plan(snapshot, new_state, join_code=join_code)
    path, rec = _operation(runtime, "bootstrap-" + str(new_state["epoch"]), value)
    args = value["tool_arguments"]
    response = None
    try:
        response = docs.batch_update_document(args["document_id"], args["requests"], args["write_control"])
    except Exception:
        pass
    try:
        readback = docs.get_document(args["document_id"])
        result = bootstrap_verify_update(value, response, readback, join_code=join_code)
    except Exception:
        _finish_operation(path, rec, status="unknown")
        raise RuntimeError("bootstrap_write_outcome_unknown_no_replay") from None
    _finish_operation(path, rec, status="verified", observed_epoch=result["epoch"],
                      response=response, readback=readback)
    return result


def _bootstrap_read(docs, active, join_code, *, require_fresh=True):
    snap = bootstrap_snapshot_from_document(docs.get_document(active["bootstrap_document_id"]),
                                            active["bootstrap_document_id"], active["bootstrap_tab_id"])
    verify_context(snap.state, join_code, active["bootstrap_document_id"], active["bootstrap_tab_id"],
                   expected_root=active["bootstrap_root"], require_fresh=require_fresh)
    return snap


@contextlib.contextmanager
def _lifecycle_lock(active_path, *, wait=False, on_wait=None):
    """Start owns the lease until launch settles; stop requests cancellation first."""
    path = Path(str(active_path) + ".lock")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "unsafe_router_lock")
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not wait: raise RuntimeError("router_lifecycle_operation_in_progress") from None
                if on_wait is not None: on_wait()
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


def _cancelled(active):
    if (Path(active["runtime"]) / "stop-requested.json").exists(): return True
    path = active.get("stop_intent_file")
    if path and Path(path).exists():
        return _load_private_json(path).get("intent_id") == active.get("lifecycle_intent")
    return False


def _check_cancelled(active):
    require(not _cancelled(active), "router_start_cancelled")


def _request_stop(active):
    path = Path(active["runtime"]) / "stop-requested.json"
    value = {"session_id": active["session_id"], "requested": int(time.time())}
    if not path.exists():
        try: write_new(path, canonical(value))
        except FileExistsError: pass
    require(_load_private_json(path)["session_id"] == active["session_id"], "router_stop_session_mismatch")


def _process_identity(pid):
    """OS start+command fingerprint. Never signal a merely reused numeric PID."""
    if type(pid) is not int or pid <= 0: return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode != 0 or not result.stdout.strip(): return None
        return hash_bytes(result.stdout.strip().encode())
    except Exception:
        return None


def _owned_process(active):
    saved = active.get("facade_identity")
    return bool(saved and _process_identity(active.get("facade_pid")) == saved)


def _stop_process(active, proc=None):
    pid = active.get("facade_pid")
    if proc is not None:
        if proc.poll() is not None: return True
        # Popen handle plus unreaped child ownership, not a persisted bare PID.
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait(timeout=5)
        return proc.poll() is not None
    if not _pid_alive(pid): return True
    if not _owned_process(active): return False
    for sig in (signal.SIGINT, signal.SIGTERM):
        if not _owned_process(active): return not _pid_alive(pid)
        try: os.kill(pid, sig)
        except ProcessLookupError: return True
        for _ in range(50):
            if not _pid_alive(pid): return True
            time.sleep(.1)
    # A changed PID is not ours to kill; do not claim process-stop verification.
    return not _pid_alive(pid)


def _pid_alive(pid):
    if type(pid) is not int or pid <= 0: return False
    try:
        os.kill(pid, 0)
        try:
            info = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True,
                                  text=True, timeout=5, check=False)
            if info.returncode == 0 and info.stdout.strip().startswith("Z"): return False
        except Exception: pass
        return True
    except OSError:
        return False


def _check_existing_active(active_path):
    if not active_path.exists(): return
    try: current = _load_private_json(active_path, 131072)
    except Exception: raise RuntimeError("router_active_state_unreadable") from None
    if current.get("closed") is True and current.get("process_stopped") is True: return
    if _pid_alive(current.get("facade_pid")):
        raise RuntimeError("router_session_already_active")
    # Pre-facade pairing may still be in progress in another terminal. Refuse to guess.
    if current.get("stage") != "ABORTED" or current.get("process_stopped") is not True:
        raise RuntimeError("router_stale_or_incomplete_active_state_requires_status")


def _join_message(active, join_code):
    selection = active.get("model_selection")
    requirement = (f"\nrequired_model={selection['model']}\n"
                   f"required_reasoning_effort={selection['reasoning_effort']}\n"
                   "请同时遵循 docs/MODEL_SELECTION.zh-CN.md：从已验证 bootstrap 的 required_selection "
                   "显式提交真实 native admission；不得回退或复用其他模型的任务。") if selection else ""
    return ("DOTS2CODEX_ROUTER_JOIN_V1\n"
            f"bootstrap_document_id={active['bootstrap_document_id']}\n"
            f"bootstrap_tab_id={active['bootstrap_tab_id']}\n"
            f"join_code={join_code}\n"
            "请按当前 Dots2Codex checkout 的 docs/ROUTER_JOIN_V1.zh-CN.md 完成本轮 router join。"
            "必须使用平台实际返回的 native task identity；不要向我索取 OAuth、pin 或 worker-config，"
            "Mac 会在独立 bootstrap Doc 中发布并在 ready 后清空当前正文；历史版本可能保留。" + requirement)


def _copy_clipboard(text):
    pbcopy = shutil.which("pbcopy")
    if not pbcopy: return False
    try:
        subprocess.run([pbcopy], input=text, text=True, check=True, timeout=5)
        return True
    except Exception:
        return False


def _wait_for(docs, active, join_code, target, *, timeout_at, poll):
    last = None
    while time.time() < timeout_at:
        _check_cancelled(active)
        snap = _bootstrap_read(docs, active, join_code); state = snap.state
        if state["stage"] != last:
            print(f"[router] bootstrap stage: {state['stage']}", flush=True); last = state["stage"]
        if target == "WORKER_ADMITTED" and state["stage"] in {"WORKER_ADMITTED", "BUNDLE_READY", "WORKER_POLLING", "CONSUMED"}:
            verify_worker_admission(state, join_code); return snap
        if target == "WORKER_POLLING" and state["stage"] in {"WORKER_POLLING", "CONSUMED"}:
            verify_worker_polling(state, join_code); return snap
        if state["stage"] in {"ABORTED", "CLOSED"}:
            raise RuntimeError("router_bootstrap_ended_before_ready")
        time.sleep(poll)
    raise RuntimeError("router_bootstrap_wait_timeout")


def _verify_reverse_probe(drive_client, bootstrap_state):
    worker = bootstrap_state["worker"]; require(worker is not None, "worker_probe_missing")
    probe = worker["probe"]
    expected_name = "dots2codex-router-probe-" + bootstrap_state["bootstrap_id"] + ".json"
    require(probe["name"] == expected_name, "worker_probe_name_mismatch")
    meta = drive_client.get_metadata(probe["file_id"])
    require(isinstance(meta, dict) and meta.get("id") == probe["file_id"] and
            meta.get("name") == expected_name and meta.get("trashed") is False and
            type(meta.get("parents")) is list and bootstrap_state["folder_id"] in meta["parents"],
            "reverse_connector_to_mac_metadata_unverified")
    raw = drive_client.get_bytes(probe["file_id"], 131072)
    require(hash_bytes(raw) == probe["sha256"], "reverse_connector_to_mac_hash_mismatch")
    try:
        value = json.loads(raw)
    except Exception:
        raise RuntimeError("reverse_connector_to_mac_probe_invalid_json") from None
    require(value == {"contract":"dots-router-probe/1", "bootstrap_id":bootstrap_state["bootstrap_id"],
                      "native_task_id":worker["native_task_id"]},
            "reverse_connector_to_mac_probe_content_mismatch")
    return {"file_id":probe["file_id"], "sha256":probe["sha256"], "bytes":len(raw)}


def _write_active(active_path, active, **changes):
    active = dict(active); active.update(changes); _private_json(active_path, active); return active


def _wait_ready(proc, ready_path, log_path, timeout=30, active=None):
    end = time.time() + timeout
    while time.time() < end:
        if active is not None: _check_cancelled(active)
        if ready_path.exists():
            return _load_private_json(ready_path, 131072)
        if proc.poll() is not None:
            raise RuntimeError("facade_exited_before_ready")
        time.sleep(.1)
    raise RuntimeError("facade_ready_timeout")


def _bridge_call(ready, journal, operation, **kwargs):
    state = _load_private_json(Path(journal) / "remote-facade-state.json", 131072)
    identity = state.get("binding") or {"session-id": "operator-before-first-turn", "thread-id": str(uuid.uuid4())}
    headers = {**identity, "Content-Type": "application/json"}
    if operation == "status": method, path, body = "GET", "/v1/bridge/status", None
    elif operation == "close": method, path, body = "POST", "/v1/bridge/close", {"confirm": True}
    else: raise ValueError("unsupported_bridge_call")
    codex_command(ready["base_url"], ".")  # strict loopback-only URL validation
    url = urlsplit(ready["base_url"]); conn = http.client.HTTPConnection(url.hostname, url.port, timeout=60)
    try:
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        resp = conn.getresponse(); raw = resp.read(2 * 1024 * 1024 + 1)
        require(len(raw) <= 2 * 1024 * 1024, "router_bridge_response_too_large")
        return resp.status, json.loads(raw)
    finally: conn.close()


def configure(args):
    # Shared read-only preflight, explicit credential reuse and atomic settings.
    # No workspace creation or config writes before Google folder MIME validation.
    from .mac_launcher import Launcher
    return Launcher(config=args.config, active=getattr(args, "active", DEFAULT_ACTIVE)).configure(args)


def _create_doc_once(drive, docs, active_path, active, config, label, *, return_snapshot=False):
    _check_cancelled(active)
    title = "Dots2Codex Router " + label + " " + active["session_id"]
    path, rec = _operation(active["runtime"], "create-" + label.lower(),
                           {"folder_id": config["folder_id"], "title": title})
    try:
        document_id = _create_workspace_doc(drive, config["folder_id"], title)
    except Exception:
        _finish_operation(path, rec, status="unknown")
        raise RuntimeError("router_document_create_unknown_no_retry") from None
    # Record the returned ID before any later read can fail.
    active = _write_active(active_path, active, **{label.lower() + "_document_id": document_id})
    _finish_operation(path, rec, status="returned", document_id=document_id)
    tab_id, resource = _blank_doc_info(docs, document_id)
    result = _write_active(active_path, active, **{label.lower() + "_tab_id": tab_id})
    return (result, resource) if return_snapshot else result


def _create_forward_probe(raw_drive, active, config):
    nonce = secrets.token_hex(24)
    raw = canonical({"contract": "dots-router-forward-probe/1",
                     "bootstrap_id": active["bootstrap_id"], "nonce": nonce})
    name = "dots2codex-router-forward-probe-" + active["bootstrap_id"] + ".json"
    file_id = raw_drive.generate_id()  # reservation only; no object write
    probe = {"file_id": file_id, "name": name, "sha256": hash_bytes(raw), "nonce": nonce}
    path, rec = _operation(active["runtime"], "create-forward-probe", probe)
    try:
        returned = raw_drive.create_bytes(config["folder_id"], name, raw, file_id)
        require(returned == file_id, "forward_probe_returned_id_mismatch")
        meta = raw_drive.get_metadata(file_id)
        require(meta.get("id") == file_id and meta.get("name") == name and meta.get("trashed") is False and
                config["folder_id"] in meta.get("parents", []), "forward_probe_metadata_mismatch")
        require(raw_drive.get_bytes(file_id, 131072) == raw, "forward_probe_raw_mismatch")
    except Exception:
        _finish_operation(path, rec, status="unknown")
        raise RuntimeError("forward_probe_outcome_unknown_no_retry") from None
    _finish_operation(path, rec, status="verified")
    return probe


def _close_control(docs, active, config):
    """Independent authoritative fence, also usable when facade is dead/unready.

    An ambiguous close is reconciled by exact durable operation ID + arguments.
    Absence is never treated as proof of failure and never causes a write replay.
    """
    pin_path = Path(active["pin_file"])
    if not active.get("control_initialization_attempted"):
        return {"closed": False, "not_created": True, "authoritative": False}
    if not pin_path.exists():
        require(not active.get("control_initialization_attempted"), "router_pin_missing_close_unverified")
        return {"closed": False, "not_created": True, "authoritative": False}
    pin = Object.parse(read_private_file(pin_path, 131072))
    require(pin.oid == active.get("deployment"), "router_close_pin_mismatch")
    store = GoogleDocsCASControlStore(docs, active["control_document_id"], active["control_tab_id"],
        active["control_id"], active["session_id"], config["mac_writer_identity"])
    binding = binding_for(pin)
    snapshot = store.read(); state = snapshot.state
    require(state["binding"] == binding, "router_close_binding_mismatch")
    path = Path(active["runtime"]) / "control-close.json"
    if path.exists():
        rec = _load_private_json(path)
        expected = rec["operation"]
        if state.get("closed") is True and expected in state["operations"]:
            _finish_operation(path, rec, status="verified")
            return {"closed": True, "authoritative": True, "operation_id": expected["id"]}
        require(rec["status"] == "rejected", "router_close_unknown_no_replay")
        # A definite CAS rejection can be replanned by a later explicit stop only.
        archive = path.with_name("control-close-rejected-" + rec["operation"]["id"] + ".json")
        os.replace(path, archive); fsync_dir(path.parent)
    if state.get("closed") is True:
        return {"closed": True, "authoritative": True, "observed_existing_fence": True}
    coordinator = SessionCoordinator(store, None)
    result = coordinator.plan("close", {"binding": binding}, uuid.uuid4().hex, snapshot=snapshot)
    expected = result["state"]["operations"][-1]
    rec = {"status": "dispatched", "operation": expected, "prior_operations": state["operations"],
           "arguments": store.prepare_update(snapshot, result["state"])}
    write_new(path, canonical(rec, max_bytes=2 * 1024 * 1024))
    try:
        store.compare_and_swap(snapshot, result["state"])
    except CASConflict:
        _finish_operation(path, rec, status="rejected")
        raise RuntimeError("router_close_cas_rejected_retry_requires_new_stop") from None
    except Exception:
        pass
    try:
        final = store.read().state
        require(final["binding"] == binding and final.get("closed") is True and
                final["operations"][:len(state["operations"])] == state["operations"] and
                expected in final["operations"], "router_close_operation_unverified")
    except Exception:
        _finish_operation(path, rec, status="unknown")
        raise RuntimeError("router_control_close_unverified_no_replay") from None
    _finish_operation(path, rec, status="verified")
    return {"closed": True, "authoritative": True, "operation_id": expected["id"]}


def _finish_bootstrap(docs, active, join_code, *, closed):
    if not active.get("bootstrap_root"): return {"verified": False, "not_initialized": True}
    snap = _bootstrap_read(docs, active, join_code, require_fresh=False)
    if snap.state["stage"] in {"CLOSED", "ABORTED"}:
        return {"verified": True, "stage": snap.state["stage"], "history_erased": False}
    if closed and snap.state["stage"] == "CONSUMED":
        nxt = close_bootstrap(snap.state, join_code=join_code)
    else:
        nxt = abort_bootstrap(snap.state, join_code=join_code, reason="mac_operator_stop")
    _bootstrap_cas(docs, snap, nxt, join_code=join_code, runtime=active["runtime"])
    return {"verified": True, "stage": nxt["stage"], "history_erased": False}


def _cleanup(docs, active_path, active, config, proc=None):
    # Close is a new safe fence, never another admit/begin or replay of unknown work.
    close_result = {"closed": False, "authoritative": False}
    errors = []
    try: close_result = _close_control(docs, active, config)
    except Exception as exc: errors.append(type(exc).__name__)
    process_stopped = _stop_process(active, proc)
    bootstrap = {"verified": False}
    try:
        pairing = _load_private_json(Path(active["runtime"]) / "private-pairing.json")
        bootstrap = _finish_bootstrap(docs, active, pairing["join_code"], closed=close_result["closed"])
    except Exception as exc: errors.append(type(exc).__name__)
    closed = close_result.get("closed") is True and close_result.get("authoritative") is True
    creation_settled = True
    for name in ("create-control", "create-bootstrap", "create-forward-probe", "initialize-bootstrap", "initialize-control"):
        path = Path(active["runtime"]) / (name + ".json")
        if path.exists():
            try: creation_settled = creation_settled and _load_private_json(path).get("status") in {"verified", "returned"}
            except Exception: creation_settled = False
    aborted = close_result.get("not_created") is True and creation_settled and (
        bootstrap.get("verified") is True or bootstrap.get("not_initialized") is True)
    stage = "CLOSED" if closed and process_stopped else "ABORTED" if aborted and process_stopped else "RECOVERY_REQUIRED"
    active = _write_active(active_path, active, stage=stage, closed=closed,
        process_stopped=process_stopped, control_close=close_result, bootstrap_cleanup=bootstrap,
        cleanup_errors=errors)
    return {"closed": closed, "stage": stage, "process_stopped": process_stopped,
            "control_close": close_result, "bootstrap_cleanup": bootstrap,
            "worker_stop_confirmed": False, "consumed_execution_may_complete": True,
            "runtime_preserved": active["runtime"], "evidence_deleted": False}


def _facade_command(active, config):
    return [sys.executable, "-m", "remote_transport.cli", "serve",
        "--pin", active["pin_file"], "--journal", active["controller_journal"],
        "--transport", "drive", "--folder-id", config["folder_id"],
        "--client-factory", "examples.google_clients:create_drive_client", "--drive-mode", "duplicate_tolerant",
        "--docs-client-factory", "examples.google_clients:create_docs_client",
        "--control-document-id", active["control_document_id"], "--control-tab-id", active["control_tab_id"],
        "--control-id", active["control_id"], "--control-writer-identity", config["mac_writer_identity"],
        "--long-session", "--deadline", str(config["deadline"]), "--poll-interval", str(config["poll_interval"]),
        "--heartbeat-interval", str(config["heartbeat_interval"]), "--port", str(config["port"]),
        "--ready", active["ready_file"]]


def _launch_facade(active_path, active, config):
    _check_cancelled(active)
    command = _facade_command(active, config)
    child = [sys.executable, "-m", "remote_transport.router_child", "--runtime", active["runtime"], "--", *command[3:]]
    proc = None
    try:
        with open(active["facade_log"], "ab", buffering=0) as log:
            proc = subprocess.Popen(child, cwd=Path(__file__).resolve().parents[1], env=dict(os.environ),
                                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # Child waits for the grant before loading facade/Google clients. PID is durable first.
        active = _write_active(active_path, active, stage="STARTING_FACADE", facade_pid=proc.pid)
        identity = _process_identity(proc.pid)
        require(identity is not None, "facade_process_identity_unverified")
        active = _write_active(active_path, active, facade_identity=identity)
        _check_cancelled(active)
        write_new(Path(active["runtime"]) / "facade-launch.json", canonical({"pid": proc.pid,
                  "session_id": active["session_id"]}))
        ready = _wait_ready(proc, Path(active["ready_file"]), Path(active["facade_log"]), active=active)
        require(ready.get("pid") == proc.pid and ready.get("deployment") == active["deployment"] and
                ready.get("session_control") == "docs_cas" and ready.get("expires") == active["pin_expires"],
                "router_facade_ready_binding_mismatch")
        selection = ready_selection(ready)
        require(selection == active.get("model_selection"), "router_facade_ready_selection_mismatch")
        codex_command(ready["base_url"], config["workdir"], selection,
                      catalog_path=ready.get("codex_model_catalog"))
        _check_cancelled(active)
        active = _write_active(active_path, active, stage="READY")
        return active, ready
    except BaseException:
        if proc is not None: _stop_process(active, proc)
        raise


def _start_locked(args, active_path, intent_id):
    expected_config = getattr(args, "expected_config_sha256", None)
    if expected_config is not None:
        # Bind the exact bytes whose credential/folder/workspace the launcher
        # validated and the operator approved, under the lifecycle lease.
        raw = read_private_file(_config_path(args.config), 131072)
        require(hash_bytes(raw) == expected_config, "router_config_changed_after_launcher_approval")
        config = _validate_config(json.loads(raw))
    else:
        config = _load_config(args.config)
    # Unsupported choices fail before Google clients, resource creation or pairing.
    selection = _resolve_selection(args, config)
    _set_google_env(config)
    _check_existing_active(active_path)
    version = subprocess.run([config["codex"], "--version"], capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip()
    require(version == config["expected_codex_version"], "codex_version_requires_live_acceptance")
    if selection is not None:
        require(version == load_catalog()["codex_version"], "selected_codex_version_requires_live_acceptance")
    from examples.google_clients import create_docs_client, create_drive_client
    from examples.remote_setup import initialize_blank
    docs = create_docs_client(); raw_drive = create_drive_client(); drive = raw_drive
    now = int(time.time()); session_id = "router-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    runtime = active_path.parent / "router-sessions" / session_id
    runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True); runtime.mkdir(mode=0o700)
    active = {"contract": "dots-router-active/2", "stage": "PREPARING", "closed": False,
        "session_id": session_id, "runtime": str(runtime), "bootstrap_id": uuid.uuid4().hex,
        "lifecycle_intent": intent_id, "stop_intent_file": str(active_path) + ".stop-intent.json",
        "bootstrap_document_id": None, "bootstrap_tab_id": None, "bootstrap_root": None,
        "control_document_id": None, "control_tab_id": None, "control_id": uuid.uuid4().hex,
        "created": now, "join_message_file": str(runtime / "join-message.txt"),
        "ready_file": str(runtime / "ready.json"), "controller_journal": str(runtime / "controller"),
        "pin_file": str(runtime / "pin.json"), "worker_config_file": str(runtime / "worker-config.json"),
        "facade_pid": None, "facade_identity": None, "facade_log": str(runtime / "facade.log"),
        "deployment": None, "native_task_id": None, "pin_expires": None,
        "model_selection": selection,
        "control_initialization_attempted": False}
    _private_json(active_path, active)
    join_code = secrets.token_urlsafe(24).rstrip("=")
    _private_json(runtime / "private-pairing.json", {"join_code": join_code})
    try:
        active = _create_doc_once(drive, docs, active_path, active, config, "Control")
        active = _create_doc_once(drive, docs, active_path, active, config, "Bootstrap")
        require(active["control_document_id"] != active["bootstrap_document_id"], "router_document_isolation_required")
        _check_cancelled(active)
        forward_probe = _create_forward_probe(raw_drive, active, config)
        initial = bootstrap_initial_state(bootstrap_id=active["bootstrap_id"], session_id=session_id,
            created=now, expires=now + config["bootstrap_ttl"], join_code=join_code,
            folder_id=config["folder_id"], control_document_id=active["control_document_id"],
            control_tab_id=active["control_tab_id"], control_id=active["control_id"],
            mac_writer_identity=config["mac_writer_identity"], worker_writer_identity=config["worker_writer_identity"],
            bootstrap_document_id=active["bootstrap_document_id"], bootstrap_tab_id=active["bootstrap_tab_id"],
            forward_probe=forward_probe, required_selection=selection)
        active = _write_active(active_path, active, bootstrap_root=root_context(initial))
        _initialize_bootstrap(docs, active["bootstrap_document_id"], active["bootstrap_tab_id"], initial, runtime)
        active = _write_active(active_path, active, stage="WAITING_FOR_WORKER")
        message = _join_message(active, join_code)
        write_new(runtime / "join-message.txt", message.encode())
        _check_cancelled(active)
        callback = getattr(args, "join_callback", None)
        if callback is not None:
            callback(active, message)
        else:
            # Join secrets never enter default logs. Copy is an explicit action.
            print("[router] private join message saved at " + active["join_message_file"], flush=True)
            if getattr(args, "copy_join", False):
                copied = _copy_clipboard(message)
                print("[router] join clipboard " + ("copied; clear after sending" if copied else "failed; use private file"), flush=True)
        snap = _wait_for(docs, active, join_code, "WORKER_ADMITTED", timeout_at=initial["expires"],
                         poll=config["bootstrap_poll_interval"])
        native_task_id = verify_worker_admission(snap.state, join_code)
        _verify_reverse_probe(raw_drive, snap.state)
        _check_cancelled(active)
        inference = {"selection": selection, "admission": snap.state["worker"]["admission"]} if selection else None
        pin = deployment(session_id, native_task_id, seconds=config["seconds"],
                         max_requests=config["max_requests"], scope=config["scope"],
                         **({"inference": inference} if inference is not None else {}))
        write_new(runtime / "pin.json", pin.raw)
        active = _write_active(active_path, active, deployment=pin.oid, native_task_id=native_task_id,
                               pin_expires=pin.body["payload"]["expires"])
        Journal.provision(runtime / "controller", pin, "controller")
        active = _write_active(active_path, active, control_initialization_attempted=True)
        op, rec = _operation(runtime, "initialize-control", {"document_id": active["control_document_id"], "pin": pin.oid})
        initialize_blank(docs, pin, active["control_document_id"], active["control_tab_id"],
                         active["control_id"], config["mac_writer_identity"])
        _finish_operation(op, rec, status="verified")
        worker_config = {"document_id": active["control_document_id"], "tab_id": active["control_tab_id"],
            "control_id": active["control_id"], "writer_identity": config["worker_writer_identity"], "folder_id": config["folder_id"]}
        raw = canonical(worker_config); write_new(runtime / "worker-config.json", raw)
        _check_cancelled(active)
        snap = _bootstrap_read(docs, active, join_code)
        nxt = bundle_ready(snap.state, join_code=join_code, pin_raw=pin.raw, config_raw=raw, deployment_hash=pin.oid)
        _bootstrap_cas(docs, snap, nxt, join_code=join_code, runtime=runtime)
        active = _write_active(active_path, active, stage="BUNDLE_READY")
        snap = _wait_for(docs, active, join_code, "WORKER_POLLING",
            timeout_at=min(initial["expires"], pin.body["payload"]["expires"]), poll=config["bootstrap_poll_interval"])
        verify_worker_polling(snap.state, join_code)
        _check_cancelled(active)
        _bootstrap_cas(docs, snap, consume_bundle(snap.state, join_code=join_code), join_code=join_code, runtime=runtime)
        active = _write_active(active_path, active, stage="CONSUMED")
        print("[router] verified worker and both raw-file directions; current bundle body cleared (history may retain it)", flush=True)
        active, ready = _launch_facade(active_path, active, config)
        return config, active, ready
    except BaseException:
        # Reload durable PID/binding, never overwrite it with a stale in-memory copy.
        active = _load_private_json(active_path)
        _cleanup(docs, active_path, active, config)
        raise


def start(args):
    active_path = _config_path(args.active)
    with _lifecycle_lock(active_path):
        intent_id = uuid.uuid4().hex
        _private_json(Path(str(active_path) + ".start-intent.json"), {"intent_id": intent_id})
        config, active, ready = _start_locked(args, active_path, intent_id)
    result = {"router_ready": True, "base_url": ready["base_url"], "runtime": active["runtime"],
              **selection_status(active.get("model_selection")),
              "facade_pid": active["facade_pid"], "closed": False}
    print("[router] ROUTER_READY " + ready["base_url"], flush=True)
    # Interactive Codex does not hold the lifecycle lock; stop remains usable.
    if args.launch_codex and sys.stdin.isatty() and not _cancelled(active):
        command = codex_command(ready["base_url"], config["workdir"], active.get("model_selection"),
                                catalog_path=ready.get("codex_model_catalog")); command[0] = config["codex"]
        result["codex_exit"] = subprocess.run(command, cwd=config["workdir"], env=dict(os.environ)).returncode
    else: result["codex_launched"] = False
    return result


def status(args):
    active_path = _config_path(args.active); require(active_path.exists(), "no_router_active_state")
    active = _load_private_json(active_path); config = _load_config(args.config); _set_google_env(config)
    # No join-code, bundles, operation arguments, credentials or log contents in status.
    out = {k: active.get(k) for k in ("session_id", "stage", "closed", "runtime", "deployment", "pin_expires")}
    out.update(selection_status(active.get("model_selection")))
    out.update(facade_alive=_pid_alive(active.get("facade_pid")), facade_owned=_owned_process(active),
               stop_requested=_cancelled(active), worker_stop_confirmed=False)
    try:
        from examples.google_clients import create_docs_client
        pairing = _load_private_json(Path(active["runtime"]) / "private-pairing.json")
        snap = _bootstrap_read(create_docs_client(), active, pairing["join_code"], require_fresh=False)
        out["bootstrap_stage"] = snap.state["stage"]; out["bootstrap_epoch"] = snap.state["epoch"]
        out["bootstrap_bundle_present"] = snap.state["bundle"] is not None
    except Exception: out["bootstrap_stage"] = "unavailable"
    if Path(active["ready_file"]).exists() and out["facade_owned"]:
        try:
            code, body = _bridge_call(_load_private_json(active["ready_file"]), active["controller_journal"], "status")
            out["bridge_http_status"] = code; out["bridge"] = body
        except Exception as exc: out["bridge_error"] = type(exc).__name__
    return out


def _signal_stop_intent(active_path):
    """Repeat while waiting: a lease owner may not have published its intent yet."""
    intent_path = Path(str(active_path) + ".start-intent.json")
    stop_path = Path(str(active_path) + ".stop-intent.json")
    if intent_path.exists():
        intent = _load_private_json(intent_path)
        if not stop_path.exists() or _load_private_json(stop_path) != intent:
            _private_json(stop_path, intent)
    if active_path.exists(): _request_stop(_load_private_json(active_path))


def stop(args):
    active_path = _config_path(args.active)
    # Never conclude "no session" from missing/stale files while another start
    # owns the lease: cancellation follows its intent as soon as it is published.
    with _lifecycle_lock(active_path, wait=True, on_wait=lambda: _signal_stop_intent(active_path)):
        _signal_stop_intent(active_path)
        require(active_path.exists(), "no_router_active_state")
        current = _load_private_json(active_path)
        _request_stop(current)
        try:
            config = _load_config(args.config); _set_google_env(config)
            from examples.google_clients import create_docs_client
            docs = create_docs_client()
        except Exception:
            # Broken settings, missing SDKs or offline auth never prevent exact
            # local shutdown. Missing authoritative closure remains explicit.
            config = {}; docs = None
        return _cleanup(docs, active_path, current, config)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("operation", choices=["configure", "start", "status", "stop", "models"])
    p.add_argument("--config", default=str(DEFAULT_CONFIG)); p.add_argument("--active", default=str(DEFAULT_ACTIVE))
    p.add_argument("--folder-id"); p.add_argument("--credentials"); p.add_argument("--workdir"); p.add_argument("--codex")
    p.add_argument("--launch-codex", action="store_true")
    p.add_argument("--copy-join", action="store_true", help="Explicitly copy this session join message; no secret stdout")
    p.add_argument("--model", "-m", help="Native model for a new session; requires --effort")
    p.add_argument("--effort", help="Explicit reasoning effort for a new session; requires --model")
    p.add_argument("--catalog", help="Supported capability snapshot JSON; never a live entitlement probe")
    a = p.parse_args(); os.umask(0o077)
    if a.operation == "models":
        require(a.model is None and a.effort is None, "models_operation_does_not_select")
        return models(a)
    if a.operation not in {"configure", "start"}:
        require(a.model is None and a.effort is None and a.catalog is None,
                "selection_flags_require_new_session_start")
    if a.operation == "configure": return configure(a)
    if a.operation == "start": return start(a)
    if a.operation == "status": return status(a)
    return stop(a)


if __name__ == "__main__":
    try:
        outcome = main()
        print(json.dumps(outcome, ensure_ascii=False, indent=2), flush=True)
        if outcome.get("stage") == "RECOVERY_REQUIRED": raise SystemExit(2)
    except Exception as exc:
        from .model import ProtocolError
        # Arbitrary exception/provider text can contain credentials or response bytes.
        print(json.dumps({"error": str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__,
                          "inspect_private_status": True}), flush=True)
        raise SystemExit(1)
