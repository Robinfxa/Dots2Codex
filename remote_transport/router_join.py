"""Offline, durable helper for an already active, trusted native endpoint.

No network, login, admission, wake, or inference occurs here. The caller obtains
its actual native identity from the platform (not cryptographic attestation).
Use one persistent private state directory for the pairing. Its reservation and
observation ledger is local fail-closed replay protection, not a distributed
one-use guarantee. Formal control CAS/begin consumption remains authoritative.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import stat
import sys
from pathlib import Path

from .backend import read_private_file, fsync_dir
from .cli import write_new
from .connector_worker import ConnectorWorker
from .control import binding_for
from .model import Object, canonical, hash_bytes, require
from .router_bootstrap import (
    snapshot_from_document, worker_admitted, worker_polling, verify_update,
    extract_bundle, plan, verify_context, root_context, context_hash, forward_probe_bytes,
)
from .session import _save
from .selection import spawn_arguments, admission_receipt, validate_admission

MAX = 2 * 1024 * 1024
DEFAULT_STATE_DIR = Path.home() / ".config" / "dots2codex" / "router-joins"


def _provider(value):
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("structuredContent"), dict):
        return value["structuredContent"]
    if isinstance(value.get("result"), dict) and any(k in value["result"] for k in ("documentId", "revisionId", "tabs", "id")):
        return value["result"]
    return value


def _read(path):
    require(path is not None, "private_evidence_file_required")
    return json.loads(read_private_file(Path(path), MAX))


def _read_code(path):
    require(path is not None, "private_join_code_file_required")
    return read_private_file(Path(path), 256).decode("ascii").strip()


def _private_dir(path):
    path = Path(path).expanduser()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    require(not path.is_symlink() and stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, "unsafe_router_state_directory")
    return path.resolve()


def _snapshot(path, document_id, tab_id):
    return snapshot_from_document(_provider(_read(path)), document_id, tab_id)


class JoinLedger:
    """Same-owner local one-attempt evidence; never reset on errors or restart."""
    def __init__(self, directory, state):
        self.root = _private_dir(directory)
        self.key = context_hash(state)
        self.path = self.root / (self.key + ".json")
        self.lock = self.root / (self.key + ".lock")
        self.context = root_context(state)

    @contextlib.contextmanager
    def locked(self):
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and
                    not info.st_mode & 0o077, "unsafe_router_ledger_lock")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.exists():
                state = _read(self.path)
                require(state.get("contract") == "dots-router-join-ledger/2" and
                        state.get("context") == self.context, "router_ledger_context_mismatch")
            else:
                state = {"contract": "dots-router-join-ledger/2", "context": self.context,
                         "last_epoch": -1, "last_hash": None, "last_revision": None, "last_events": [],
                         "native_task_id": None, "forward_probe": None, "probe": None,
                         "materialization": None, "operations": {}}
                self.save(state)
            yield state
        finally:
            os.close(fd)

    def save(self, value):
        _save(self.path, value)

    def observe(self, value, snap, native_task_id=None):
        state = snap.state; digest = hash_bytes(canonical(state))
        require(root_context(state) == self.context and state["epoch"] >= value["last_epoch"] and
                (state["epoch"] != value["last_epoch"] or digest == value["last_hash"]) and
                state["events"][:len(value["last_events"])] == value["last_events"],
                "bootstrap_observation_rollback_or_fork")
        if native_task_id:
            require(value["native_task_id"] in (None, native_task_id), "bootstrap_local_native_identity_changed")
            if state["worker"] is not None:
                require(state["worker"]["native_task_id"] == native_task_id,
                        "bootstrap_native_identity_mismatch")
            value["native_task_id"] = native_task_id
        value.update(last_epoch=state["epoch"], last_hash=digest, last_revision=snap.revision_id, last_events=state["events"])
        self.save(value)

    def prepare(self, value, operation, snap, nxt, join_code, destination):
        require(operation not in value["operations"], "bootstrap_operation_already_issued_no_replay")
        require(destination and not Path(destination).exists(), "new_router_plan_path_required")
        packet = plan(snap, nxt, join_code=join_code)
        raw = canonical(packet)
        record = {"operation_id": packet["operation_id"], "plan_sha256": hash_bytes(raw),
                  "path": str(Path(destination).absolute()), "status": "issued_outcome_unknown",
                  "evidence": None}
        # Persist consumption before making the external action packet available.
        value["operations"][operation] = record; self.save(value)
        write_new(destination, raw)
        return {"plan_saved": destination, "operation_id": packet["operation_id"],
                "stage": nxt["stage"], "tool_arguments": packet["tool_arguments"],
                "one_attempt_only": True, "retry_on_unknown": False}


def verify_forward_probe(state, metadata, raw):
    """Raw-byte path required. A Docs read or extracted text is not equivalent."""
    probe = state["forward_probe"]; meta = _provider(metadata)
    require(isinstance(meta, dict) and meta.get("id") == probe["file_id"] and
            meta.get("title") == probe["name"] and meta.get("mime_type") == "application/json" and
            type(meta.get("parent_ids")) is list and all(isinstance(v, str) for v in meta["parent_ids"])
            and state["folder_id"] in meta["parent_ids"],
            "forward_mac_to_connector_metadata_unverified")
    # The connected Drive tool does not expose trash state. Do not synthesize it.
    require("trashed" not in meta or meta["trashed"] is False, "forward_probe_trashed")
    expected = forward_probe_bytes(state["bootstrap_id"], probe["nonce"])
    require(raw == expected and hash_bytes(raw) == probe["sha256"],
            "forward_mac_to_connector_raw_mismatch")
    return {"file_id": probe["file_id"], "sha256": probe["sha256"], "bytes": len(raw),
            "metadata_sha256": hash_bytes(canonical(meta)), "context_hash": context_hash(state),
            "trash_state_verified": False}


def _source_hashes():
    release = Path(__file__).resolve().parents[1]
    files = ("remote_transport/connector_cell.py", "remote_transport/connector_worker.py",
             "native_connector/runner.js", "native_connector/tool_adapter.js",
             "remote_transport/selection.py", "remote_transport/native_capabilities.json")
    hashes = {}
    for name in files:
        path = release / name
        require(path.is_file() and not path.is_symlink(), "router_parallel_runtime_source_missing")
        raw = path.read_bytes()
        require(0 < len(raw) <= MAX, "router_parallel_runtime_source_invalid")
        hashes[name] = hash_bytes(raw)
    return hashes


def _execution_guidance(root, native_task_id):
    common = ["--root", str(root), "--native-task-id", native_task_id]
    manifest = str(Path(root) / "router-empty-manifest.json")
    return {"execution_mode": "router_parallel_cells_v1", "native_execution_required": True,
            "source_hashes": _source_hashes(), "initial_manifest": manifest,
            "claim_begin_command": ["python3", "-m", "remote_transport.connector_cell", "claim-begin",
                *common, "--manifest", manifest, "--save", "NEW_PRIVATE_CLAIM_CELL.js"],
            "upload_commit_command_template": ["python3", "-m", "remote_transport.connector_cell", "upload-commit",
                *common, "--manifest", "CURRENT_EVIDENCE_MANIFEST.json", "--seq", "N", "--save", "NEW_PRIVATE_UPLOAD_CELL.js"],
            "instructions": "Observe REQUESTED read-only after paced poll before claim-begin; let that cell own fresh read/tick/CAS. Its success ends at fetch_request_once. Then fetch exact request/predecessor raw evidence, consume input --expose-path once, and deliver all hash-checked contiguous input-chunk segments to the same native context before inference. Use upload-commit for the parallel verification barrier; do not substitute serial upload/tick."}


def _verify_runtime(record, state, root, native_task_id):
    require(record is not None and record.get("status") == "materialized" and
            record.get("root") == str(Path(root).resolve()) and
            record.get("bundle_hashes") == state["bundle_hashes"] and
            record.get("native_task_id") == native_task_id, "router_materialization_evidence_required")
    require(record.get("execution_mode") == "router_parallel_cells_v1" and
            record.get("source_hashes") == _source_hashes(), "router_parallel_runtime_source_changed")
    worker = ConnectorWorker(root, native_task_id)
    with worker.locked() as worker_state:
        require(worker_state.get("router_execution_mode") == "router_parallel_cells_v1" and
                worker_state.get("router_source_hashes") == record["source_hashes"],
                "router_parallel_runtime_mode_mismatch")
    pin_raw = read_private_file(Path(root) / "pin.json", MAX)
    config_raw = read_private_file(Path(root) / "config.json", MAX)
    require(hash_bytes(pin_raw) == state["bundle_hashes"]["pin_sha256"] and
            hash_bytes(config_raw) == state["bundle_hashes"]["config_sha256"] and
            worker.pin.oid == state["bundle_hashes"]["deployment_hash"] and
            worker.pin.body["identity"]["session_id"] == state["session_id"],
            "router_runtime_raw_bundle_mismatch")
    receipt = _read(Path(root) / "router-materialization.json")
    require(receipt == record, "router_runtime_materialization_receipt_mismatch")
    return worker


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    require(not any(arg == "--join-code" or arg.startswith("--join-code=") for arg in argv),
            "join_code_argument_removed_use_private_file")
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("operation", choices=["inspect", "prepare-probe", "verify-forward-probe", "plan-admit",
                                        "verify", "materialize", "plan-ready", "plan-native", "record-native"])
    p.add_argument("--snapshot"); p.add_argument("--bootstrap-snapshot"); p.add_argument("--control-snapshot")
    p.add_argument("--document-id"); p.add_argument("--tab-id"); p.add_argument("--join-code-file", required=True)
    p.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    p.add_argument("--native-task-id"); p.add_argument("--writer-identity")
    p.add_argument("--probe-file-id"); p.add_argument("--probe-name"); p.add_argument("--probe-sha256")
    p.add_argument("--probe-metadata"); p.add_argument("--probe-raw")
    p.add_argument("--root"); p.add_argument("--plan-file"); p.add_argument("--response"); p.add_argument("--readback")
    p.add_argument("--save")
    p.add_argument("--task-name"); p.add_argument("--message-file")
    p.add_argument("--actual-arguments"); p.add_argument("--native-result"); p.add_argument("--admission-receipt")
    a = p.parse_args(argv); os.umask(0o077)
    join_code = _read_code(a.join_code_file)

    if a.operation == "verify":
        packet = _read(a.plan_file)
        expected = packet["expected_state"]; args = packet["tool_arguments"]
        verify_context(expected, join_code, args["document_id"], packet["tab_id"])
        ledger = JoinLedger(a.state_dir, expected)
        with ledger.locked() as saved:
            records = [r for r in saved["operations"].values() if r["operation_id"] == packet["operation_id"]]
            require(len(records) == 1 and records[0]["plan_sha256"] == hash_bytes(canonical(packet)) and
                    records[0]["path"] == str(Path(a.plan_file).absolute()), "router_operation_evidence_required")
            record = records[0]
            response = None if a.response is None else _read(a.response)
            readback = _read(a.readback)
            try:
                current = verify_update(packet, response, readback, join_code=join_code)
                fresh = snapshot_from_document(_provider(readback), args["document_id"], packet["tab_id"])
                ledger.observe(saved, fresh)
            except Exception:
                # Unknown is durable and cannot be converted into an emitted retry.
                record["status"] = "outcome_unknown_no_replay"; ledger.save(saved)
                raise
            record["status"] = "verified"
            record["evidence"] = {"readback_sha256": hash_bytes(canonical(_provider(readback))),
                                  "revision_id": fresh.revision_id, "epoch": current["epoch"],
                                  "event": current["events"][expected["epoch"] - 1],
                                  "response_sha256": None if response is None else hash_bytes(canonical(_provider(response)))}
            ledger.save(saved)
        return {"verified": True, "stage": current["stage"], "epoch": current["epoch"],
                "operation_id": packet["operation_id"], "reconciled_from_event": response is None}

    snap_path = a.snapshot or a.bootstrap_snapshot
    require(snap_path and a.document_id and a.tab_id, "bootstrap_snapshot_configuration_required")
    snap = _snapshot(snap_path, a.document_id, a.tab_id)
    state = snap.state; verify_context(state, join_code, a.document_id, a.tab_id)
    ledger = JoinLedger(a.state_dir, state)
    with ledger.locked() as saved:
        ledger.observe(saved, snap, a.native_task_id)
        if a.operation == "inspect":
            return {"stage": state["stage"], "session_id": state["session_id"], "expires": state["expires"],
                    "bootstrap_id": state["bootstrap_id"], "context_hash": context_hash(state),
                    "control": state["control"], "folder_id": state["folder_id"],
                    "forward_probe": state["forward_probe"], "required_selection": state.get("required_selection"),
                    "native_admission_tool": "collaboration.spawn_agent",
                    "selection_mode": "explicit" if "required_selection" in state else "legacy_unverified",
                    "native_task_id":
                    None if state["worker"] is None else state["worker"]["native_task_id"]}
        if a.operation in {'plan-native', 'record-native'}:
            require(state['stage'] == 'WAITING_FOR_WORKER' and 'required_selection' in state,
                    'selected_waiting_bootstrap_required')
            if a.operation == 'plan-native':
                require(saved.get('native_admission') is None and a.save and not Path(a.save).exists(),
                        'native_admission_already_reserved_no_replay')
                message = read_private_file(Path(a.message_file), MAX).decode('utf-8') if a.message_file else None
                arguments = spawn_arguments(state['required_selection'], a.task_name, message)
                packet = {'contract': 'dots-native-admission-plan/1', 'context_hash': context_hash(state),
                          'tool': 'collaboration.spawn_agent', 'arguments': arguments,
                          'selection': state['required_selection']}
                saved['native_admission'] = {'status': 'reserved_outcome_unknown',
                    'plan_sha256': hash_bytes(canonical(packet)), 'plan_path': str(Path(a.save).absolute()),
                    'receipt': None}
                ledger.save(saved)  # never issue the native call twice, including after a crash
                write_new(a.save, canonical(packet))
                return {'plan_file': a.save, 'tool': packet['tool'], 'arguments': arguments,
                        'native_invoked_by_python': False, 'one_attempt_only': True,
                        'next': 'Trusted parent calls the actual native tool once; record exact arguments and result. Never retry unknown admission.'}
            pending = saved.get('native_admission')
            require(pending and pending['status'] == 'reserved_outcome_unknown', 'native_admission_plan_required')
            packet = _read(a.plan_file)
            require(str(Path(a.plan_file).absolute()) == pending['plan_path'] and
                    hash_bytes(canonical(packet)) == pending['plan_sha256'] and
                    packet['context_hash'] == context_hash(state), 'native_admission_plan_mismatch')
            actual, result = _read(a.actual_arguments), _read(a.native_result)
            require(actual == packet['arguments'], 'native_admission_arguments_mismatch')
            require(isinstance(result, dict) and not result.get('error') and isinstance(result.get('task_name'), str),
                    'successful_native_admission_result_required')
            receipt = admission_receipt(state['required_selection'], actual, result['task_name'])
            require(a.save and not Path(a.save).exists(), 'new_admission_receipt_path_required')
            require(saved['native_task_id'] in (None, receipt['native_task_id']), 'bootstrap_local_native_identity_changed')
            pending.update(status='recorded', receipt=receipt, result_sha256=hash_bytes(canonical(result)),
                           arguments_sha256=hash_bytes(canonical(actual)))
            saved['native_task_id'] = receipt['native_task_id']; ledger.save(saved)
            write_new(a.save, canonical(receipt))
            return {'admission_receipt': a.save, 'native_task_id': receipt['native_task_id'],
                    'selection': state['required_selection'], 'underlying_model_verified': False,
                    'verification': receipt['verification']}
        require(a.native_task_id, "native_task_id_required")
        require(state["stage"] not in {"CLOSED", "ABORTED", "CONSUMED"}, "bootstrap_pairing_finished")
        if a.operation == "verify-forward-probe":
            evidence = verify_forward_probe(state, _read(a.probe_metadata),
                                             read_private_file(Path(a.probe_raw), MAX))
            saved["forward_probe"] = evidence; ledger.save(saved)
            return {"forward_probe_verified": True, **evidence}
        if a.operation == "prepare-probe":
            require(state["stage"] == "WAITING_FOR_WORKER" and a.save,
                    "probe_requires_waiting_bootstrap_and_save")
            require(saved["probe"] is None, "router_probe_already_prepared_no_replay")
            raw = canonical({"contract": "dots-router-probe/1", "bootstrap_id": state["bootstrap_id"],
                             "native_task_id": a.native_task_id})
            value = {"probe_file": str(Path(a.save).absolute()), "file_name":
                     "dots2codex-router-probe-" + state["bootstrap_id"] + ".json",
                     "sha256": hash_bytes(raw), "folder_id": state["folder_id"], "mime_type": "application/json"}
            saved["probe"] = value; ledger.save(saved)
            write_new(a.save, raw)
            return value
        if a.operation == "plan-admit":
            require(a.writer_identity == state["control"]["worker_writer_identity"], "worker_writer_identity_mismatch")
            require(saved["probe"] is not None and a.probe_name == saved["probe"]["file_name"] and
                    a.probe_sha256 == saved["probe"]["sha256"], "worker_probe_local_evidence_required")
            admission = None
            if 'required_selection' in state:
                admission = _read(a.admission_receipt)
                validate_admission(admission, state['required_selection'], a.native_task_id)
                recorded = saved.get('native_admission')
                require(recorded and recorded['status'] == 'recorded' and recorded['receipt'] == admission,
                        'parent_native_admission_record_required')
            nxt = worker_admitted(state, join_code=join_code, native_task_id=a.native_task_id, admission=admission,
                                  probe={"file_id": a.probe_file_id, "name": a.probe_name, "sha256": a.probe_sha256})
            return ledger.prepare(saved, "admit", snap, nxt, join_code, a.save)
        if a.operation == "materialize":
            require(a.root and state["stage"] == "BUNDLE_READY", "worker_runtime_and_fresh_bundle_required")
            admission = saved["operations"].get("admit")
            require(admission is not None and admission["status"] == "verified" and
                    admission["evidence"] is not None and admission["evidence"]["event"] in state["events"] and
                    admission["operation_id"] == state["events"][0]["operation_id"],
                    "worker_admission_operation_unverified")
            pin_raw, config_raw = extract_bundle(state, join_code=join_code, native_task_id=a.native_task_id)
            require(saved["forward_probe"] is not None and
                    saved["forward_probe"]["context_hash"] == context_hash(state), "forward_probe_evidence_required")
            if saved["materialization"] is not None:
                _verify_runtime(saved["materialization"], state, a.root, a.native_task_id)
                return {"materialized": True, "resumed_existing": True, **saved["materialization"],
                        **_execution_guidance(a.root, a.native_task_id)}
            root = str(Path(a.root).resolve())
            # A crash after this point consumes this reservation. Never provision
            # a second root or reconstruct state from a copied stale bundle.
            record = {"status": "materializing_outcome_unknown", "root": root,
                      "context_hash": context_hash(state), "native_task_id": a.native_task_id,
                      "bundle_hashes": state["bundle_hashes"], "source_hashes": _source_hashes(),
                      "execution_mode": "router_parallel_cells_v1",
                      "runtime_hash": hash_bytes(canonical({"root": root, "context_hash": context_hash(state),
                          "native_task_id": a.native_task_id, "bundle_hashes": state["bundle_hashes"]}))}
            saved["materialization"] = record; ledger.save(saved)
            worker = ConnectorWorker.provision(root, Object.parse(pin_raw), json.loads(config_raw),
                                               a.native_task_id, poll_seconds=15)
            with worker.locked() as worker_state:
                worker_state["router_execution_mode"] = "router_parallel_cells_v1"
                worker_state["router_source_hashes"] = record["source_hashes"]
                worker.save(worker_state)
            write_new(Path(root) / "router-empty-manifest.json", canonical([]))
            record["status"] = "materialized"
            write_new(Path(root) / "router-materialization.json", canonical(record))
            ledger.save(saved)
            return {"materialized": True, "worker_runtime": root, "runtime_hash": record["runtime_hash"],
                    "deployment": worker.pin.oid, "connector_status": worker.status(),
                    **_execution_guidance(root, a.native_task_id)}
        require(a.operation == "plan-ready" and a.root and a.control_snapshot,
                "ready_requires_worker_runtime_and_control_snapshot")
        require(saved["forward_probe"] is not None and saved["forward_probe"]["sha256"] == state["forward_probe"]["sha256"]
                and saved["forward_probe"]["context_hash"] == context_hash(state), "forward_probe_evidence_required")
        extract_bundle(state, join_code=join_code, native_task_id=a.native_task_id)
        worker = _verify_runtime(saved["materialization"], state, a.root, a.native_task_id)
        control_resource = _provider(_read(a.control_snapshot))
        control_state = worker.store.snapshot_from_document(control_resource).state
        require(control_state["binding"] == binding_for(worker.pin) and
                control_state["phase"] == "IDLE" and not control_state.get("closed", False),
                "worker_control_not_idle_or_verified")
        action = worker.tick(control_resource, [])
        require(action.get("action") == "wait" and action.get("phase") == "IDLE",
                "worker_control_not_idle_or_verified")
        nxt = worker_polling(state, join_code=join_code, native_task_id=a.native_task_id,
                             runtime_hash=saved["materialization"]["runtime_hash"])
        result = ledger.prepare(saved, "ready", snap, nxt, join_code, a.save)
        return {**result, "runtime_hash": saved["materialization"]["runtime_hash"]}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except Exception as exc:
        from .model import ProtocolError
        print(json.dumps({"error": str(exc) if isinstance(exc, ProtocolError) else type(exc).__name__}))
        raise SystemExit(1)
