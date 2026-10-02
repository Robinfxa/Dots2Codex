"""Explicit operator helpers: inspect access, or initialize ONE blank control doc.

init-control is a cloud write. Do not run without approval of the exact document.
This module never creates resources, grants access, starts login, or runs a model.
"""
import argparse
import json
import time
from remote_transport import Object
from remote_transport.backend import read_private_file
from remote_transport.control import GoogleDocsCASControlStore, _document_text, initial_state, block_for, dispatch_docs_write
from remote_transport.model import ProtocolError, require
from .google_clients import create_drive_client, create_docs_client


def initialize_blank(client, pin, document_id, tab_id, control_id, writer_identity, *, return_snapshot=False):
    """Initialize once and verify once; optionally return that exact readback.

    The default operator-facing result is unchanged. A composing caller may use
    the verified snapshot immediately instead of issuing a duplicate GET.
    """
    document = client.get_document(document_id)
    require(document.get("documentId") == document_id, "control_document_mismatch")
    require(_document_text(document, tab_id) == "\n", "control_document_must_be_blank")
    revision = document.get("revisionId")
    require(isinstance(revision, str) and revision, "editable_revision_required")
    state = initial_state(pin, control_id)
    block = block_for(state)
    deadline = time.monotonic() + max(0, state['binding']['expires'] - time.time())
    def dispatch_check():
        require(state['binding']['created'] <= time.time() < state['binding']['expires'],
                'control_deployment_expired')
    # Preserve Docs' mandatory terminal newline. No deletion or repair operation.
    response = dispatch_docs_write(client, document_id,
        [{"insertText": {"location": {"index": 1, "tabId": tab_id}, "text": block[:-1]}}],
        {"requiredRevisionId": revision}, deadline=deadline, check=dispatch_check)
    require(isinstance(response, dict) and response.get("documentId") == document_id
            and isinstance(response.get("replies"), list) and len(response["replies"]) == 1,
            "control_initialization_unverified")
    control = GoogleDocsCASControlStore(client, document_id, tab_id, control_id,
        pin.body["identity"]["session_id"], writer_identity)
    verified = control.read()
    require(verified.state == state, "control_initialization_readback_mismatch")
    if return_snapshot:
        return verified
    return {"initialized": True, "phase": "IDLE", "native_execution_authorized": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["check-access", "init-control"])
    parser.add_argument("--folder-id", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--tab-id", required=True)
    parser.add_argument("--pin")
    parser.add_argument("--control-id")
    parser.add_argument("--writer-identity")
    args = parser.parse_args()
    docs = create_docs_client()
    if args.operation == "check-access":
        metadata = create_drive_client().get_metadata(args.folder_id)
        require(metadata.get("id") == args.folder_id and metadata.get("trashed") is False,
                "folder_read_access_not_verified")
        document = docs.get_document(args.document_id)
        require(document.get("documentId") == args.document_id, "control_document_mismatch")
        _document_text(document, args.tab_id)
        require(isinstance(document.get("revisionId"), str) and document["revisionId"],
                "editable_revision_required")
        return {"folder_metadata_readable": True, "control_document_readable": True,
                "tab_verified": True, "upload_or_CAS_write_tested": False,
                "cross_app_message_access_tested": False}
    require(args.pin and args.control_id and args.writer_identity, "pin_and_control_identity_required")
    pin = Object.parse(read_private_file(args.pin, 131072))
    return initialize_blank(docs, pin, args.document_id, args.tab_id,
                            args.control_id, args.writer_identity)


if __name__ == "__main__":
    try:
        print(json.dumps(main()))
    except Exception:
        # Keep endpoint IDs, source document text, and credentials out of CLI output.
        print(json.dumps({"error": "setup_failed_preserve_state_and_inspect_privately"}))
        raise SystemExit(1)
