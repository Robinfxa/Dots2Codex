import json
import tempfile
import unittest
from pathlib import Path

from remote_transport.model import canonical, hash_bytes
from remote_transport.router_mac import _validate_config, _join_message, _verify_reverse_probe


class _FakeDrive:
    def __init__(self, raw, name, folder):
        self.raw=raw; self.name=name; self.folder=folder
    def get_metadata(self, file_id):
        return {"id":file_id,"name":self.name,"parents":[self.folder],"trashed":False}
    def get_bytes(self, file_id, limit):
        assert len(self.raw) <= limit
        return self.raw


class RouterMacPureTests(unittest.TestCase):
    def config(self, root):
        return {
            "folder_id":"folder", "authorized_user_file":str(root/"auth.json"),
            "workdir":str(root/"work"), "mac_writer_identity":"mac-controller-router",
            "worker_writer_identity":"remote-worker-router", "seconds":14400,
            "max_requests":128, "scope":"responses_tools", "port":0, "deadline":1800,
            "poll_interval":5, "heartbeat_interval":15, "bootstrap_ttl":1800,
            "bootstrap_poll_interval":5, "codex":"codex", "expected_codex_version":"codex-cli 0.159.2"}

    def test_default_shape_is_valid(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            self.assertEqual(_validate_config(self.config(root))["max_requests"],128)

    def test_join_message_is_single_copy_paste_and_contains_no_bundle(self):
        active={"bootstrap_document_id":"doc123","bootstrap_tab_id":"t.0"}
        message=_join_message(active,"AbCdEfGhIjKlMnOpQrStUvWxYz012345")
        self.assertIn("DOTS2CODEX_ROUTER_JOIN_V1",message)
        self.assertIn("bootstrap_document_id=doc123",message)
        self.assertIn("join_code=",message)
        self.assertNotIn("pin_b64",message)
        self.assertNotIn("authorized-user",message)

    def test_reverse_connector_probe_is_exact(self):
        raw=canonical({"contract":"dots-router-probe/1","bootstrap_id":"boot123",
                       "native_task_id":"/root/native"})
        name="dots2codex-router-probe-boot123.json"
        state={"bootstrap_id":"boot123","folder_id":"folder",
               "worker":{"native_task_id":"/root/native","probe":{"file_id":"file123","name":name,
                           "sha256":hash_bytes(raw)}}}
        evidence=_verify_reverse_probe(_FakeDrive(raw,name,"folder"),state)
        self.assertEqual(evidence["bytes"],len(raw))
        bad=json.loads(json.dumps(state)); bad["worker"]["probe"]["sha256"]="0"*64
        with self.assertRaises(Exception):
            _verify_reverse_probe(_FakeDrive(raw,name,"folder"),bad)


if __name__ == "__main__": unittest.main()
