"""Direct global Codex selection using main's reviewed transaction primitives.

Only the Direct-owned top-level selector/catalog and provider table are changed.
The formatter, snapshots, cooperative lock, atomic commit and syntax-aware
three-way restore come from dots_lite.config_transaction, not a second writer.
No API key, auth.json, project/profile settings, or web_search preference is read
or changed. The explicitly approved local HTTP token is never an API key.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import secrets
import time
import tomllib
from urllib.parse import urlsplit

from dots_lite import client_catalog as catalog
from dots_lite import config_transaction as tx
from dots_lite.private_io import private_dir, private_write, read_private_file, strict_json
from dots_lite.protocol import ProtocolError, canonical, hash_bytes, require

CONTRACT = "dots-direct-global-config/1"
PROVIDER = "dots2codex_direct_global"
OWNED = tuple((key,) for key in ("model_provider", "model", "model_reasoning_effort", "model_catalog_json")) + (("model_providers", PROVIDER),)
ACTIVE_PHASES = ("prepared", "committed", "restore_prepared")
WARNINGS = [
    "The selected CODEX_HOME/config.toml applies to ordinary CLI and Desktop launches only when both clients use that home.",
    "Fully quit/reopen each client and start a new thread. Existing threads, profiles, project settings, CLI overrides and managed settings may retain or override another provider.",
    "A bounded local bridge bearer is persisted in config.toml and its private transaction postimage. It is not a tunnel/control-plane API key. No Terminal environment inheritance is assumed.",
    "Config and transaction files containing the local bearer use mode 0600. Restore retains this privacy tightening and never relaxes file permissions.",
    "Source compatibility is pinned to codex-cli 0.159.2; the installed Desktop engine and a real native roundtrip remain unverified.",
    "Existing web_search is preserved. Hosted search, Responses Lite and live reasoning-effort updates are unsupported; explicitly enabling them may fail closed.",
    "Cooperative locks cannot exclude a noncooperating editor racing the final atomic replace.",
]


def default_selection():
    return catalog.select(catalog.load_catalog(), "gpt-6-astra", "xhigh")


def _pair(value):
    require(isinstance(value, dict), "invalid_model_effort_pair")
    return catalog.select(catalog.load_catalog(), value.get("model"), value.get("reasoning_effort"))


def catalog_for_pairs(allowed_pairs, default_pair):
    """Reuse main's verified model metadata/instructions for allowed exact pairs."""
    require(isinstance(allowed_pairs, list) and 1 <= len(allowed_pairs) <= 64, "invalid_allowed_model_pairs")
    selected = [_pair(pair) for pair in allowed_pairs]
    identities = [(pair["model"], pair["reasoning_effort"]) for pair in selected]
    require(len(identities) == len(set(identities)), "duplicate_model_effort_pair")
    default = _pair(default_pair)
    require((default["model"], default["reasoning_effort"]) in identities, "default_pair_not_allowed")
    models = {}
    for pair in selected:
        model = catalog.catalog_for_selection(pair)["models"][0]
        name = pair["model"]
        if name not in models:
            model["description"] = "Dots2Codex Direct; each new route admits its exact allowed model and effort."
            model["supported_reasoning_levels"] = []
            models[name] = model
        models[name]["supported_reasoning_levels"].append({"effort": pair["reasoning_effort"], "description": "Allowed exact Direct route admission effort"})
        if name == default["model"]:
            models[name]["default_reasoning_level"] = default["reasoning_effort"]
    return {"models": list(models.values())}


def validate_catalog(path, allowed_pairs, default_pair):
    path = Path(path).expanduser().absolute()
    require(not any(p.is_symlink() for p in (path, *path.parents)), "symlink_path_rejected")
    raw = read_private_file(path, catalog.MAX_CATALOG_BYTES)
    require(raw == canonical(catalog_for_pairs(allowed_pairs, default_pair)), "direct_catalog_selection_mismatch")
    return str(path)


def write_catalog(path, allowed_pairs, default_pair):
    """Create immutable mode-0600 catalog using main private file primitives."""
    path = Path(path).expanduser().absolute()
    private_dir(path.parent)
    raw = canonical(catalog_for_pairs(allowed_pairs, default_pair))
    # Same immutable publication pattern as main client_catalog.write_catalog.
    temporary = path.parent / (".direct-catalog-" + secrets.token_hex(12))
    private_write(temporary, raw)
    try:
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            pass
        tx.fsync_dir(path.parent)
    finally:
        temporary.unlink()
    return validate_catalog(path, allowed_pairs, default_pair)


def _validated_info(info):
    require(isinstance(info, dict), "direct_config_info_required")
    selected = catalog.validate_selection(info.get("selection", default_selection()))
    allowed = info.get("allowed_pairs", [{"model": selected["model"], "reasoning_effort": selected["reasoning_effort"]}])
    pair = {"model": selected["model"], "reasoning_effort": selected["reasoning_effort"]}
    catalog_path = validate_catalog(info.get("catalog_path", ""), allowed, pair)
    ident = info.get("config_id")
    require(isinstance(ident, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", ident), "invalid_direct_config_id")
    token = info.get("local_bearer")
    require(isinstance(token, str) and 16 <= len(token) <= 512 and token.isascii()
            and all(33 <= ord(char) <= 126 for char in token), "invalid_local_bridge_bearer")
    # A local bearer never authorizes a remotely reachable origin or redirect.
    try:
        url = urlsplit(info.get("base_url", ""))
        valid = (url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
                 and url.netloc == f"127.0.0.1:{url.port}" and url.path == "/v1"
                 and not url.query and not url.fragment)
    except (ValueError, TypeError):
        valid = False
    require(valid, "direct_loopback_base_url_required")
    return selected, catalog_path


def new_values(info):
    selected, catalog_path = _validated_info(info)
    return {"model_provider": PROVIDER, "model": selected["model"],
            "model_reasoning_effort": selected["reasoning_effort"],
            "model_catalog_json": catalog_path, "model_providers": {PROVIDER: {
                "name": "Dots2Codex Direct global", "base_url": info["base_url"],
                "wire_api": "responses", "requires_openai_auth": False,
                "supports_websockets": False, "request_max_retries": 0,
                "stream_max_retries": 0,
                "http_headers": {"Authorization": "Bearer " + info["local_bearer"]}}}}


def render_patch(raw, info):
    doc, plain = tx.decoded(raw)
    desired = new_values(info)
    providers = plain.get("model_providers", {})
    require(isinstance(providers, dict), "unsupported_provider_table")
    existing = providers.get(PROVIDER)
    require(existing is None or isinstance(existing, dict), "unsupported_provider_table")
    require(existing is None or set(existing) <= set(desired["model_providers"][PROVIDER]),
            "existing_owned_provider_has_unmanaged_fields")
    for path in OWNED:
        tx.set_at(doc, path, tx.value_at(desired, path))
    after = tx.parser().dumps(doc)
    if "\r\n" in raw.decode("utf-8"):
        require("\n" not in raw.decode("utf-8").replace("\r\n", ""), "mixed_config_newlines_not_supported")
        after = after.replace("\r\n", "\n").replace("\n", "\r\n")
    result = after.encode("utf-8")
    expected = copy.deepcopy(plain)
    for path in OWNED:
        if len(path) == 1:
            expected[path[0]] = tx.value_at(desired, path)
        else:
            expected.setdefault(path[0], {})[path[1]] = tx.value_at(desired, path)
    require(tomllib.loads(after) == expected, "config_semantics_changed_outside_owned_patch")
    require(len(result) <= tx.MAX_CONFIG_BYTES, "config_too_large")
    return result


def preview(state_dir, codex_home, info):
    home = tx.known_home(codex_home)
    before = tx.snapshot(home / "config.toml")
    after = render_patch(before["raw"], info)
    _, plain = tx.decoded(before["raw"])
    desired = new_values(info)
    changes = []
    for path in OWNED:
        new = tx.value_at(desired, path)
        if tx.value_at(plain, path) != new:
            if path == ("model_providers", PROVIDER):
                new = copy.deepcopy(new)
                new["http_headers"]["Authorization"] = "Bearer <local bridge credential hidden>"
            changes.append(".".join(path) + ": <prior value hidden> -> " + json.dumps(new, ensure_ascii=False))
    return {"contract": CONTRACT, "config_path": str(home / "config.toml"),
            "config_id": info["config_id"], "before_hash": before["hash"],
            "after_hash": hash_bytes(after), "before_exists": before["exists"],
            "diff": "\n".join(changes), "warnings": list(WARNINGS),
            "privacy_change_required": before["exists"] and before["mode"] != 0o600,
            "credential_persistence_required": True, "credential_kind": "bounded_local_bridge_bearer_only",
            "changes": before["raw"] != after, "write_performed": False,
            "client_compatibility_verified": False, "real_roundtrip_verified": False}


def _directory(state_dir, *, create=False):
    state = private_dir(state_dir)
    path = state / "config-transactions"
    if not create and not path.exists():
        return None
    return private_dir(path, create=create)


def _journal_path(state_dir, tid):
    require(isinstance(tid, str) and re.fullmatch(r"[0-9a-f]{32}", tid), "invalid_transaction_id")
    directory = _directory(state_dir)
    require(directory is not None, "config_transaction_not_found")
    return directory / (tid + ".json")


def load_transaction(state_dir, tid):
    journal = _journal_path(state_dir, tid)
    value = strict_json(read_private_file(journal, tx.MAX_CONFIG_BYTES * 3))
    require(value.get("contract") == CONTRACT and value.get("id") == tid, "invalid_config_transaction")
    require(value.get("owned_paths") == [list(path) for path in OWNED], "invalid_transaction_owned_paths")
    require(value.get("phase") in ACTIVE_PHASES + ("restored", "aborted"), "unknown_transaction_phase")
    directory = journal.parent
    require(value.get("backup") == str(directory / (tid + ".before"))
            and value.get("postimage") == str(directory / (tid + ".after")), "transaction_file_binding_mismatch")
    home = tx.known_home(value["codex_home"])
    require(value.get("config_path") == str(home / "config.toml"), "transaction_target_mismatch")
    tx.check_transaction_target(value)
    before = read_private_file(value["backup"], tx.MAX_CONFIG_BYTES)
    after = read_private_file(value["postimage"], tx.MAX_CONFIG_BYTES)
    require(hash_bytes(before) == value["before_hash"] and hash_bytes(after) == value["after_hash"], "transaction_backup_hash_mismatch")
    return journal, value, before, after


def _records(state_dir):
    directory = _directory(state_dir)
    if directory is None:
        return []
    values = []
    for path in sorted(directory.glob("*.json")):
        _, value, _, _ = load_transaction(state_dir, path.stem)
        values.append(value)
    return values


def status(state_dir, codex_home=None):
    home = tx.known_home(codex_home) if codex_home is not None else None
    records = []
    for value in _records(state_dir):
        if home is not None and value["codex_home"] != str(home):
            continue
        current = tx.snapshot(value["config_path"])
        active = value["phase"] in ACTIVE_PHASES
        records.append({"transaction_id": value["id"], "phase": value["phase"],
                        "config_path": value["config_path"], "config_id": value["config_id"],
                        "active": active, "restore_required": active,
                        "config_matches_applied": current["exists"] and current["hash"] == value["after_hash"],
                        "created": value["created"]})
    active = [record for record in records if record["active"]]
    require(len(active) <= 1, "multiple_active_config_transactions_require_review")
    return {"contract": CONTRACT, "transactions": records,
            "active_transaction": active[0] if active else None, "write_performed": False}


def apply(state_dir, codex_home, info, *, expected_before_hash, expected_after_hash,
          confirm=False, approve_private_config=False, check_ready=None,
          before_commit=None, after_replace=None):
    require(confirm is True, "explicit_direct_config_confirmation_required")
    require(callable(check_ready), "local_services_recheck_required")
    def verify_ready():
        result = check_ready()
        require(result is True or isinstance(result, dict) and result.get("ready") is True,
                "local_services_not_verified_ready")
    verify_ready()
    home = tx.known_home(codex_home)
    home_id = tx.home_identity(home)
    with tx.home_lock(home):
        path = home / "config.toml"
        before = tx.snapshot(path)
        require(before["hash"] == expected_before_hash, "preview_outdated")
        after = render_patch(before["raw"], info)
        require(hash_bytes(after) == expected_after_hash, "preview_outdated")
        require(not before["exists"] or before["mode"] == 0o600 or approve_private_config is True,
                "explicit_private_config_permission_confirmation_required")
        prior = [v for v in _records(state_dir) if v["config_path"] == str(path) and v["phase"] in ACTIVE_PHASES]
        if prior:
            require(len(prior) == 1, "multiple_active_config_transactions_require_review")
            old = prior[0]
            require(old["phase"] == "committed" and old["config_id"] == info["config_id"]
                    and before["raw"] == after and old["after_hash"] == before["hash"],
                    "existing_config_transaction_requires_reconciliation")
            repaired = before["mode"] != 0o600
            if repaired:
                def recheck_existing():
                    if before_commit:
                        before_commit()
                    tx.check_transaction_target(old)
                    verify_ready()
                written = tx.commit(path, dict(before, mode=0o600), after,
                                    before_commit=recheck_existing, after_replace=after_replace)
                old["after_identity"] = written["identity"]
                tx.save_manifest(_journal_path(state_dir, old["id"]), old)
            return {"transaction_id": old["id"], "phase": "committed", "config_path": str(path),
                    "write_performed": repaired, "already_applied": True,
                    "private_permissions_repaired": repaired, "restart_required": True}
        require(before["raw"] != after, "unowned_matching_config_requires_review")
        _, prior_plain = tx.decoded(before["raw"])
        require(PROVIDER not in prior_plain.get("model_providers", {}), "unowned_direct_provider_requires_review")
        directory = _directory(state_dir, create=True)
        tid = secrets.token_hex(16)
        backup, postimage = directory / (tid + ".before"), directory / (tid + ".after")
        private_write(backup, before["raw"])
        private_write(postimage, after)
        manifest = {"contract": CONTRACT, "id": tid, "phase": "prepared",
                    "config_path": str(path), "codex_home": str(home), "home_identity": home_id,
                    "owned_paths": [list(path) for path in OWNED], "config_id": info["config_id"],
                    "created": time.time(), "before_hash": before["hash"], "after_hash": hash_bytes(after),
                    "before_exists": before["exists"], "before_identity": before["identity"],
                    "before_mode": before["mode"], "backup": str(backup), "postimage": str(postimage)}
        journal = directory / (tid + ".json")
        tx.save_manifest(journal, manifest)
        def final_check():
            if before_commit:
                before_commit()
            tx.check_transaction_target(manifest)
            verify_ready()
        # main commit uses expected['mode'] for the replacement but compares its
        # original identity/hash. Tightening therefore happens atomically.
        expected = dict(before, mode=0o600)
        written = tx.commit(path, expected, after, before_commit=final_check, after_replace=after_replace)
        manifest.update(phase="committed", after_identity=written["identity"], committed_at=time.time())
        tx.save_manifest(journal, manifest)
        return {"transaction_id": tid, "phase": "committed", "config_path": str(path),
                "write_performed": True, "restart_required": True, "auth_file_touched": False,
                "web_search_changed": False, "client_compatibility_verified": False,
                "real_roundtrip_verified": False}


def reconcile(state_dir, tid):
    journal, value, before, after = load_transaction(state_dir, tid)
    with tx.home_lock(Path(value["codex_home"])):
        tx.check_transaction_target(value)
        current = tx.snapshot(value["config_path"])
        phase = value["phase"]
        if phase in ("restored", "aborted"):
            return {"phase": phase, "write_performed": False}
        if phase == "restore_prepared":
            if current["exists"] == value["restore_exists"] and current["hash"] == value["restore_hash"]:
                value["phase"] = "restored"
            elif current["exists"] and current["hash"] == value.get("restore_from_hash"):
                value["phase"] = "committed"  # Known pre-write interruption; retry safely.
            else:
                raise ProtocolError("restore_outcome_requires_manual_reconciliation")
        elif current["exists"] and current["hash"] == value["after_hash"]:
            value.update(phase="committed", after_identity=current["identity"])
        elif phase == "prepared" and current["exists"] == value["before_exists"] and current["hash"] == value["before_hash"]:
            value["phase"] = "aborted"
        elif phase == "committed":
            # User edits after a committed write belong to the three-way restore.
            return {"phase": "committed", "write_performed": False, "config_matches_applied": False}
        else:
            raise ProtocolError("config_outcome_requires_manual_reconciliation")
        tx.check_transaction_target(value)
        tx.save_manifest(journal, value)
        return {"phase": value["phase"], "write_performed": False}


def restore(state_dir, tid, *, confirm=False, before_commit=None, after_replace=None):
    require(confirm is True, "explicit_restore_confirmation_required")
    journal, value, before, after = load_transaction(state_dir, tid)
    with tx.home_lock(Path(value["codex_home"])):
        tx.check_transaction_target(value)
        if value["phase"] == "restored":
            return {"phase": "restored", "write_performed": False, "restart_required": True}
        require(value["phase"] == "committed", "reconcile_transaction_before_restore")
        path = Path(value["config_path"])
        current = tx.snapshot(path)
        require(current["exists"], "config_deleted_after_takeover")
        exact = current["raw"] == after
        restored = before if exact else tx.restore_bytes(before, after, current["raw"], owned_paths=OWNED)
        delete = exact and not value["before_exists"]
        value.update(phase="restore_prepared", restore_hash=hash_bytes(restored),
                     restore_exists=not delete, restore_from_hash=current["hash"])
        tx.check_transaction_target(value)
        tx.save_manifest(journal, value)
        def final_check():
            if before_commit:
                before_commit()
            tx.check_transaction_target(value)
        tx.commit(path, dict(current, mode=0o600), restored, delete=delete,
                  before_commit=final_check, after_replace=after_replace)
        value["phase"] = "restored"
        tx.save_manifest(journal, value)
        return {"phase": "restored", "write_performed": True,
                "mode": "exact" if exact else "three_way_owned_values", "restart_required": True,
                "running_clients_stopped": False, "catalogs_deleted": False,
                "private_permissions_retained": not delete}
