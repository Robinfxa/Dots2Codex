"""Isolated offline tests. No Google, auth, native admission, or inference calls."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from remote_transport.model import deployment, canonical, hash_bytes, ProtocolError
from remote_transport.control import initial_state as control_initial_state, block_for as control_block_for
from remote_transport.router_bootstrap import (
    initial_state, block_for, decode_block, snapshot_from_document, prepare_update,
    verify_update, worker_admitted, verify_worker_admission, bundle_ready, extract_bundle,
    worker_polling, verify_worker_polling, consume_bundle, close_bootstrap, abort_bootstrap,
    join_code_hash, root_context, context_hash, verify_context, verify_join_code, plan,
    forward_probe_bytes,
)
from remote_transport.router_join import main as join_main, verify_forward_probe, JoinLedger


def doc(document_id, tab_id, revision, text):
    return {"documentId": document_id, "revisionId": revision,
            "suggestionsViewMode": "SUGGESTIONS_INLINE", "tabs": [{"tabId": tab_id,
            "parentTabId": None, "body": {"content": [{"sectionBreak": {}},
            {"paragraph": {"elements": [{"textRun": {"content": text}}]}}]}}]}


def private_json(path, value):
    path.write_bytes(canonical(value)); path.chmod(0o600)


def response(revision="r2"):
    return {"documentId": "bootDoc", "replies": [{"replaceAllText": {"occurrencesChanged": 1}}],
            "writeControl": {"requiredRevisionId": revision}}


class RouterBootstrapFixtures:
    def setUp(self):
        self.code = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
        self.now = int(time.time()) - 10
        self.native = "/root/serve_router"
        self.forward_raw = forward_probe_bytes("boot123", "n" * 32)
        self.forward = {"file_id": "forwardFile", "name": "dots2codex-router-forward-probe-boot123.json",
                        "sha256": hash_bytes(self.forward_raw), "nonce": "n" * 32}
        self.state = initial_state(bootstrap_id="boot123", session_id="router-session", created=self.now,
            expires=self.now + 1800, join_code=self.code, folder_id="folder_1",
            control_document_id="controlDoc", control_tab_id="t.0", control_id="control123",
            mac_writer_identity="mac-controller-router", worker_writer_identity="remote-worker-router",
            bootstrap_document_id="bootDoc", bootstrap_tab_id="t.0", forward_probe=self.forward)
        self.probe = {"file_id": "probeFile", "name": "dots2codex-router-probe-boot123.json",
            "sha256": hash_bytes(canonical({"contract": "dots-router-probe/1", "bootstrap_id": "boot123",
                                            "native_task_id": self.native}))}
        self.config = {"document_id": "controlDoc", "tab_id": "t.0", "control_id": "control123",
                       "writer_identity": "remote-worker-router", "folder_id": "folder_1"}

    def admit(self):
        return worker_admitted(self.state, join_code=self.code, native_task_id=self.native,
                               probe=self.probe, now=self.now + 1)

    def ready(self, admitted=None):
        admitted = admitted or self.admit()
        pin = deployment("router-session", self.native, seconds=14400, max_requests=128,
                         scope="responses_tools", now=self.now + 2)
        ready = bundle_ready(admitted, join_code=self.code, pin_raw=pin.raw,
                             config_raw=canonical(self.config), deployment_hash=pin.oid,
                             now=max(self.now + 3, admitted["worker"]["admitted_at"]))
        return ready, pin

    def stages(self):
        admitted = self.admit(); ready, pin = self.ready(admitted)
        polling = worker_polling(ready, join_code=self.code, native_task_id=self.native,
                                 runtime_hash="a" * 64, now=self.now + 4)
        consumed = consume_bundle(polling, join_code=self.code, now=self.now + 5)
        return [self.state, admitted, ready, polling, consumed], pin

    def snapshot(self, state, revision="r1"):
        return snapshot_from_document(doc("bootDoc", "t.0", revision, block_for(state)), "bootDoc", "t.0")

class RouterBootstrapTests(RouterBootstrapFixtures, unittest.TestCase):
    def test_roundtrip_root_is_bound_without_plaintext_code(self):
        block = block_for(self.state)
        self.assertEqual(decode_block(block), self.state)
        self.assertNotIn(self.code, block)
        self.assertIn(join_code_hash(self.code), block)
        self.assertEqual(verify_context(self.state, self.code, "bootDoc", "t.0"), self.state)

    def test_all_immutable_root_fields_authenticated(self):
        mutations = {"bootstrap_id": "other", "session_id": "other", "folder_id": "other",
                     "bootstrap_document_id": "other", "bootstrap_tab_id": "other",
                     "created": self.now - 1, "expires": self.now + 1799}
        for key, value in mutations.items():
            with self.subTest(key=key):
                changed = copy.deepcopy(self.state); changed[key] = value
                with self.assertRaises(ProtocolError): verify_join_code(changed, self.code)
        for key in self.state["control"]:
            changed = copy.deepcopy(self.state); changed["control"][key] = "other"
            with self.assertRaises(ProtocolError): verify_join_code(changed, self.code)
        changed = copy.deepcopy(self.state); changed["forward_probe"]["file_id"] = "other"
        with self.assertRaises(ProtocolError): verify_join_code(changed, self.code)

    def test_actual_doc_tab_and_local_root_anchors(self):
        with self.assertRaises(ProtocolError): verify_context(self.state, self.code, "copiedDoc", "t.0")
        with self.assertRaises(ProtocolError): verify_context(self.state, self.code, "bootDoc", "other")
        bad = root_context(self.state); bad["folder_id"] = "other"
        with self.assertRaises(ProtocolError):
            verify_context(self.state, self.code, "bootDoc", "t.0", expected_root=bad)
        with self.assertRaises(ProtocolError):
            snapshot_from_document(doc("copy", "t.0", "r1", block_for(self.state)), "copy", "t.0")

    def test_full_lifecycle_and_bound_ack(self):
        states, pin = self.stages(); admitted, ready, polling, consumed = states[1:]
        self.assertEqual(verify_worker_admission(admitted, self.code), self.native)
        self.assertEqual(extract_bundle(ready, join_code=self.code, native_task_id=self.native),
                         (pin.raw, canonical(self.config)))
        ack = verify_worker_polling(polling, self.code)
        self.assertEqual(ack["bundle_hashes"], ready["bundle_hashes"])
        self.assertEqual(ack["forward_probe_sha256"], self.forward["sha256"])
        self.assertIsNone(consumed["bundle"])
        self.assertIsNotNone(consumed["events"][1]["after"]["bundle_commitment"])
        closed = close_bootstrap(consumed, join_code=self.code, now=self.now + 6)
        self.assertEqual(closed["stage"], "CLOSED")
        verify_join_code(closed, self.code)

    def test_expiry_at_all_pairing_boundaries(self):
        states, _ = self.stages(); _, admitted, ready, polling, _ = states
        expired = self.state["expires"]
        calls = [lambda: verify_context(self.state, self.code, "bootDoc", "t.0", now=expired),
            lambda: worker_admitted(self.state, join_code=self.code, native_task_id=self.native,
                                     probe=self.probe, now=expired),
            lambda: verify_worker_admission(admitted, self.code, now=expired),
            lambda: extract_bundle(ready, join_code=self.code, native_task_id=self.native, now=expired),
            lambda: worker_polling(ready, join_code=self.code, native_task_id=self.native,
                                   runtime_hash="b" * 64, now=expired),
            lambda: verify_worker_polling(polling, self.code, now=expired),
            lambda: consume_bundle(polling, join_code=self.code, now=expired)]
        for call in calls:
            with self.assertRaises(ProtocolError): call()
        with self.assertRaises(ProtocolError):
            verify_context(self.state, self.code, "bootDoc", "t.0", now=self.now - 1)

    def test_expiry_still_allows_authenticated_cleanup(self):
        states, _ = self.stages()
        closed = close_bootstrap(states[4], join_code=self.code, now=self.state["expires"] + 1)
        packet = plan(self.snapshot(states[4]), closed, join_code=self.code)
        self.assertEqual(verify_update(packet, response(), doc("bootDoc", "t.0", "r2", block_for(closed)),
            join_code=self.code, now=self.state["expires"] + 1)["stage"], "CLOSED")
        aborted = abort_bootstrap(states[2], join_code=self.code, reason="offline", now=self.state["expires"] + 1)
        self.assertEqual(aborted["stage"], "ABORTED"); self.assertIsNone(aborted["bundle"])

    def test_three_fast_peer_advances_are_reconciled(self):
        states, _ = self.stages()
        for index in (1, 2, 3):
            with self.subTest(transition=index):
                packet = plan(self.snapshot(states[index - 1]), states[index], join_code=self.code)
                self.assertEqual(packet["tool_arguments"]["write_control"], {"requiredRevisionId": "r1"})
                current = verify_update(packet, response("r2"),
                    doc("bootDoc", "t.0", "r3", block_for(states[index + 1])), join_code=self.code)
                self.assertEqual(current, states[index + 1])

    def test_unknown_response_exact_event_is_reconciled_without_replay(self):
        states, _ = self.stages()
        packet = plan(self.snapshot(states[0]), states[1], join_code=self.code)
        self.assertEqual(verify_update(packet, None, doc("bootDoc", "t.0", "r9", block_for(states[4])),
                                       join_code=self.code), states[4])
        with self.assertRaisesRegex(ProtocolError, "no_replay"):
            verify_update(packet, None, doc("bootDoc", "t.0", "r2", block_for(states[0])), join_code=self.code)

    def test_competing_signed_transition_does_not_prove_our_operation(self):
        ours, other = self.admit(), self.admit()
        self.assertNotEqual(ours["events"][0]["operation_id"], other["events"][0]["operation_id"])
        packet = plan(self.snapshot(self.state), ours, join_code=self.code)
        with self.assertRaisesRegex(ProtocolError, "no_replay"):
            verify_update(packet, response(), doc("bootDoc", "t.0", "r2", block_for(other)), join_code=self.code)

    def test_invalid_response_never_relaxes_cas_evidence(self):
        admitted = self.admit(); packet = plan(self.snapshot(self.state), admitted, join_code=self.code)
        for result in ({}, response("r1"), {**response(), "documentId": "other"},
                       {**response(), "replies": [{"replaceAllText": {"occurrencesChanged": True}}]},
                       {**response(), "replies": [{"replaceAllText": {"occurrencesChanged": 0}}]}):
            with self.assertRaises(ProtocolError):
                verify_update(packet, result, doc("bootDoc", "t.0", "r2", block_for(admitted)), join_code=self.code)

    def test_tampered_transition_and_plan_rejected(self):
        admitted = self.admit()
        bad = copy.deepcopy(admitted); bad["events"][0]["mac"] = "0" * 64
        with self.assertRaises(ProtocolError): verify_join_code(bad, self.code)
        packet = plan(self.snapshot(self.state), admitted, join_code=self.code)
        packet["tool_arguments"]["write_control"]["requiredRevisionId"] = "wrong"
        with self.assertRaises(ProtocolError):
            verify_update(packet, response("wrong"), doc("bootDoc", "t.0", "r2", block_for(admitted)), join_code=self.code)
        bad = copy.deepcopy(admitted); bad["events"][0]["after"]["worker"]["native_task_id"] = "/root/other"
        with self.assertRaises(ProtocolError): verify_join_code(bad, self.code)

    def test_bundle_raw_integrity_and_identity_checks(self):
        admitted = self.admit(); ready, pin = self.ready(admitted)
        bad = copy.deepcopy(ready); bad["bundle"]["pin_b64"] = bad["bundle"]["pin_b64"][:-4] + "AAAA"
        with self.assertRaises(ProtocolError): extract_bundle(bad, join_code=self.code, native_task_id=self.native)
        for session, native in (("other", self.native), ("router-session", "/root/other")):
            wrong = deployment(session, native, now=self.now + 2, seconds=600)
            with self.assertRaises(ProtocolError):
                bundle_ready(admitted, join_code=self.code, pin_raw=wrong.raw, config_raw=canonical(self.config),
                             deployment_hash=wrong.oid, now=self.now + 3)
        for raw, digest in ((pin.raw, "a" * 64), (pin.raw + b"\n", pin.oid)):
            with self.assertRaises(ProtocolError):
                bundle_ready(admitted, join_code=self.code, pin_raw=raw, config_raw=canonical(self.config),
                             deployment_hash=digest, now=self.now + 3)
        for key in self.config:
            wrong = dict(self.config); wrong[key] = "other"
            with self.assertRaises(ProtocolError):
                bundle_ready(admitted, join_code=self.code, pin_raw=pin.raw, config_raw=canonical(wrong),
                             deployment_hash=pin.oid, now=self.now + 3)

    def test_signed_expired_pin_is_rejected(self):
        admitted = self.admit()
        pin = deployment("router-session", self.native, now=self.now + 2, seconds=1)
        with self.assertRaises(ProtocolError):
            bundle_ready(admitted, join_code=self.code, pin_raw=pin.raw, config_raw=canonical(self.config),
                         deployment_hash=pin.oid, now=self.now + 4)

    def test_forward_raw_path_exact_bytes_metadata_and_nonce(self):
        meta = {"id": self.forward["file_id"], "title": self.forward["name"], "mime_type": "application/json", "parent_ids": ["folder_1"]}
        self.assertEqual(verify_forward_probe(self.state, meta, self.forward_raw)["sha256"], self.forward["sha256"])
        for key, value in (("id", "wrong"), ("title", "wrong"), ("mime_type", "text/plain"), ("parent_ids", ["wrong"]), ("trashed", True)):
            with self.assertRaises(ProtocolError): verify_forward_probe(self.state, {**meta, key: value}, self.forward_raw)
        with self.assertRaises(ProtocolError): verify_forward_probe(self.state, meta, self.forward_raw + b"\n")

    def test_bootstrap_and_control_must_be_distinct(self):
        changed = copy.deepcopy(self.state)
        changed["bootstrap_document_id"] = changed["control"]["document_id"]
        with self.assertRaisesRegex(ProtocolError, "must_differ"):
            block_for(changed)

    def test_strict_document_shape_preserved(self):
        resource = doc("bootDoc", "t.0", "r1", block_for(self.state))
        resource["tabs"].append(copy.deepcopy(resource["tabs"][0]))
        with self.assertRaises(ProtocolError): snapshot_from_document(resource, "bootDoc", "t.0")
        with self.assertRaises(ProtocolError): decode_block(block_for(self.state).replace('"epoch":0', '"epoch":0,"epoch":0'))


class RouterJoinDurabilityTests(RouterBootstrapFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name); self.serial = 0
        self.codefile = self.directory / "join-code.txt"
        self.codefile.write_text(self.code); self.codefile.chmod(0o600)
        self.ledgerdir = self.directory / "ledger"

    def file(self, value, name=None):
        self.serial += 1; path = self.directory / (name or f"evidence-{self.serial}.json")
        private_json(path, value); return path

    def call(self, op, state=None, **args):
        argv = [op, "--join-code-file", str(self.codefile), "--state-dir", str(self.ledgerdir)]
        if state is not None:
            snap = self.file(doc("bootDoc", "t.0", f"r{state['epoch'] + 1}", block_for(state)))
            argv += ["--snapshot", str(snap), "--document-id", "bootDoc", "--tab-id", "t.0",
                     "--native-task-id", self.native]
        for key, value in args.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        return join_main(argv)

    def admission_plan(self):
        probe = self.call("prepare-probe", self.state, save=self.directory / "probe.json")
        packet_path = self.directory / "admit-plan.json"
        self.call("plan-admit", self.state, writer_identity=self.config["writer_identity"],
                  probe_file_id="probeFile", probe_name=probe["file_name"], probe_sha256=probe["sha256"], save=packet_path)
        packet = json.loads(packet_path.read_bytes()); return packet_path, packet["expected_state"]

    def verified_bundle(self):
        packet, admitted = self.admission_plan(); ready, pin = self.ready(admitted)
        fresh = self.file(doc("bootDoc", "t.0", "r3", block_for(ready)))
        self.call("verify", plan_file=packet, readback=fresh)  # lost response, advanced peer
        raw = self.directory / "forward.json"; raw.write_bytes(self.forward_raw); raw.chmod(0o600)
        metadata = self.file({"id": self.forward["file_id"], "title": self.forward["name"],
                              "parent_ids": ["folder_1"], "mime_type": "application/json"})
        self.call("verify-forward-probe", ready, probe_metadata=metadata, probe_raw=raw)
        return ready, pin

    def test_cli_rejects_join_code_argument_and_broad_secret_file(self):
        with mock.patch("sys.stderr"), self.assertRaises(ProtocolError):
            join_main(["inspect", "--join-code", self.code])
        self.codefile.chmod(0o644)
        with self.assertRaises(ProtocolError): self.call("inspect", self.state)

    def test_cli_root_tampering_before_probe_upload_rejected(self):
        changed = copy.deepcopy(self.state); changed["folder_id"] = "other"
        snap = self.file(doc("bootDoc", "t.0", "r1", block_for(changed)))
        with self.assertRaises(ProtocolError):
            self.call("prepare-probe", snapshot=snap, document_id="bootDoc", tab_id="t.0",
                      native_task_id=self.native, save=self.directory / "bad-probe.json")
        self.assertFalse((self.directory / "bad-probe.json").exists())

    def test_cli_expired_inspect_probe_materialize_ready_rejected(self):
        ready, _ = self.ready()
        with mock.patch("remote_transport.router_bootstrap.time.time", return_value=self.state["expires"]):
            for op, state in (("inspect", self.state), ("prepare-probe", self.state),
                              ("materialize", ready), ("plan-ready", ready)):
                with self.assertRaises(ProtocolError): self.call(op, state)

    def test_plan_consumption_prevents_duplicate_emission(self):
        packet, admitted = self.admission_plan()
        with self.assertRaisesRegex(ProtocolError, "no_replay"):
            self.call("plan-admit", self.state, writer_identity=self.config["writer_identity"],
                      probe_file_id="probeFile", probe_name=self.probe["name"], probe_sha256=self.probe["sha256"],
                      save=self.directory / "second-plan.json")
        self.assertFalse((self.directory / "second-plan.json").exists())
        original = self.file(doc("bootDoc", "t.0", "r2", block_for(self.state)))
        with self.assertRaisesRegex(ProtocolError, "no_replay"):
            self.call("verify", plan_file=packet, readback=original)
        ledger = JoinLedger(self.ledgerdir, self.state)
        with ledger.locked() as saved:
            self.assertEqual(saved["operations"]["admit"]["status"], "outcome_unknown_no_replay")

    def test_readback_rollback_detected_after_advance(self):
        ready, _ = self.verified_bundle()
        with self.assertRaisesRegex(ProtocolError, "rollback"):
            self.call("inspect", self.state)

    def test_higher_epoch_signed_fork_is_rejected(self):
        admitted = self.admit()
        self.call("inspect", admitted)
        alternate = self.admit(); branch, _ = self.ready(alternate)
        with self.assertRaisesRegex(ProtocolError, "rollback_or_fork"):
            self.call("inspect", branch)

    def test_parallel_execution_mode_and_source_hashes_are_required(self):
        ready, pin = self.verified_bundle(); root = self.directory / "runtime"
        result = self.call("materialize", ready, root=root)
        self.assertEqual(result["execution_mode"], "router_parallel_cells_v1")
        self.assertEqual(len(result["source_hashes"]), 6)
        self.assertIn("remote_transport.connector_cell", result["claim_begin_command"])
        worker_state = json.loads((root / "worker.json").read_bytes())
        self.assertEqual(worker_state["router_execution_mode"], "router_parallel_cells_v1")
        worker_state["router_execution_mode"] = "serial"
        private_json(root / "worker.json", worker_state)
        control = self.file(doc("controlDoc", "t.0", "c1", control_block_for(control_initial_state(pin, "control123"))))
        with self.assertRaisesRegex(ProtocolError, "mode_mismatch"):
            self.call("plan-ready", ready, root=root, control_snapshot=control, save=self.directory / "ready.json")

    def test_materialization_reservation_blocks_second_root_and_supports_exact_resume(self):
        ready, _ = self.verified_bundle(); root = self.directory / "runtime"
        first = self.call("materialize", ready, root=root)
        self.assertTrue(first["materialized"])
        second = self.call("materialize", ready, root=root)
        self.assertTrue(second["resumed_existing"])
        with self.assertRaises(ProtocolError): self.call("materialize", ready, root=self.directory / "other-runtime")
        self.assertFalse((self.directory / "other-runtime").exists())

    def test_materialization_interruption_is_not_reprovisioned(self):
        ready, _ = self.verified_bundle(); root = self.directory / "runtime"
        with mock.patch("remote_transport.router_join.ConnectorWorker.provision", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError): self.call("materialize", ready, root=root)
        with self.assertRaises(ProtocolError): self.call("materialize", ready, root=root)
        self.assertFalse(root.exists())

    def test_ready_requires_forward_evidence_exact_runtime_and_control(self):
        ready, pin = self.verified_bundle(); root = self.directory / "runtime"
        self.call("materialize", ready, root=root)
        control = self.file(doc("controlDoc", "t.0", "c1", control_block_for(control_initial_state(pin, "control123"))))
        plan_path = self.directory / "ready-plan.json"
        result = self.call("plan-ready", ready, root=root, control_snapshot=control, save=plan_path)
        self.assertEqual(result["stage"], "WORKER_POLLING")
        packet = json.loads(plan_path.read_bytes()); polling = packet["expected_state"]
        consumed = consume_bundle(polling, join_code=self.code)
        verified = self.call("verify", plan_file=plan_path,
            response=self.file(response("r4")), readback=self.file(doc("bootDoc", "t.0", "r5", block_for(consumed))))
        self.assertEqual(verified["stage"], "CONSUMED")
        with self.assertRaises(ProtocolError): self.call("materialize", ready, root=root)

    def test_missing_forward_probe_prevents_materialization(self):
        packet, admitted = self.admission_plan(); ready, _ = self.ready(admitted)
        self.call("verify", plan_file=packet, readback=self.file(doc("bootDoc", "t.0", "r3", block_for(ready))))
        with self.assertRaisesRegex(ProtocolError, "forward_probe"):
            self.call("materialize", ready, root=self.directory / "runtime")

    def test_tampered_runtime_cannot_generate_ready_ack(self):
        ready, pin = self.verified_bundle(); root = self.directory / "runtime"
        self.call("materialize", ready, root=root)
        config = dict(self.config); config["folder_id"] = "other"
        private_json(root / "config.json", config)
        control = self.file(doc("controlDoc", "t.0", "c1", control_block_for(control_initial_state(pin, "control123"))))
        with self.assertRaisesRegex(ProtocolError, "raw_bundle"):
            self.call("plan-ready", ready, root=root, control_snapshot=control, save=self.directory / "ready.json")


if __name__ == "__main__":
    unittest.main()
