# Router repaired-merge validation (2026-10-01 UTC)

## Verdict and provenance

This is an isolated, offline-validated candidate merged onto published
`689efa055e6bb2d290f8870e25ff1ebb3a3109bd`. It preserves every published public path
and the newer parallel connector/file/timing implementation omitted by the old
Router ZIP. No installation, OAuth grant, live Google resource write, native
admission/inference, active-runtime replacement or GitHub publication was performed
for this candidate.

The input ZIP SHA-256 was
`42d5008e1287fa3ee30e822a1e5eb4787d0465c857c8fb7100c879ea6cc143e6`.
The packaged manifest identifies exact candidate file lengths/hashes and defines
the public-source tree digest. The archive hash is supplied separately because
an archive cannot contain its own final hash.

## Repairs and exercised safety properties

- A full immutable bootstrap root authenticates nonce/session, actual Doc/tab,
  separate Control Doc, folder, identities, expiry and forward raw-file probe.
  Pin/config hashes, native identity and polling acknowledgment remain bound
- Signed logical transition histories carry random operation IDs. A later,
  authenticated successor can prove the exact earlier write even if the peer
  advances before readback. A sibling, old ack, missing operation or changed
  history does not prove success. Unknown writes are never blindly replayed
- Same-owner private pairing ledger records observations, one-attempt plans,
  materialization reservations and verified evidence. It detects local rollback,
  forks and repeated/new-root materialization within that preserved ledger.
  It is not a global cryptographic one-use registry or platform attestation
- Mac-to-connector raw-file verification and connector-to-Mac verification precede
  readiness. They check exact file IDs, names, parents, raw hashes and probe
  payloads. Connector metadata does not expose trash state; no trash proof is invented
- Router worker results explicitly direct the parallel upload cell. A result CAS
  requires the preserved all-verified upload batch. Source hashes are checked
  before ready and cell generation, preventing accidental mixed-release use
- Stop follows a start's durable intent while waiting for the lifecycle lease,
  including first-ever startup and replacement before active/intent publication.
  It cannot return success while an earlier start later resumes into READY
- A child launch gate waits for durable PID/OS-identity recording before facade
  import. Readiness failure/cancellation reaps known children; reused numeric
  PIDs are not signalled. A dead facade does not skip authoritative Control CAS close
- Close remains unverified on missing/unreadable/unknown authority. Local process
  stop, bootstrap cleanup and native-worker stop are reported separately. Unknown
  closure is read-only reconciled by exact operation ID, never replayed
- One broad regression exposed a pre-existing in-process facade observation race:
  the controller request index could be visible just before its matching facade
  job. The merge now holds the existing RLock across enqueue publication. A
  deterministic barrier test proves readers wait; strict index validation remains
- Routine status/error outputs do not echo join-code, bundle or arbitrary provider
  log contents. Startup deliberately prints/copies the private join message; its
  code remains in terminal/clipboard and private local pairing files until the
  user handles them. Join-code CLI arguments are rejected in favor of a private
  file. Installer environments and private Router state are excluded from
  packaging/source freeze

## Reproducible offline checks

Run from a fresh extracted checkout:

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s remote_tests -v
python3 -B -m unittest discover -s remote_audit/cas -v
python3 -B -m unittest discover -s remote_audit/drive -v
python3 -B -m unittest discover -s sdk_tests -v
node --test native_connector/runner.test.js native_connector/test_adapter.js
node native_connector/benchmark.js
```

The SDK audit requires the optional Google dependencies but does not use real
credentials or network. In this workspace it used an already-present audited
package directory; no packages were installed. The plain system interpreter's
initial SDK collection attempt lacked those optional dependencies; rerunning with
that existing dependency directory passed.

Final clean-package results are recorded in the delivered validation summary:
134 original tests, 209 remote tests (including the 128-request synthetic stress
case), 47 CAS audits, 48 Drive audits, 3 actual-SDK offline tests, and 25 Node tests.
This is 441 Python test executions and 25 Node tests. Independent adversarial
review additionally covers the pairing and lifecycle interleavings. Python syntax,
launcher shell syntax, internal document links, manifest hashes and archive
extraction are checked separately.

The synthetic parallel benchmark remains 866 ms versus 444 ms under its fixed
virtual schedule: 12 RPCs, 2 fresh control reads and 4 external writes in both
paths. The 1.95 ratio measures scheduling overlap only, not a new live speedup.

## What earlier live evidence does and does not establish

The unchanged long-session baseline was previously observed with official
`codex-cli 0.159.2`: two dependent text turns, a real Mac workspace-tool cycle,
four model admissions and final receipt/explicit close. That run preceded this
Router and did not validate its automated pairing, launch or close paths.

This candidate's whole-startup tests use in-memory provider ports and local
synthetic child processes. They do not establish real Google tool compatibility,
Mac OS behavior, actual credential/folder access, native task availability or
long-duration survival. Full new Router live acceptance is still required.

## Explicit remaining limits

- Default four hours / 128 model requests; hard maximum eight hours / 128 requests
- Manual actual-platform native admission; no automatic wake or OAuth handoff
- HMAC integrity/code possession, not encryption or platform identity attestation
- Clearing the current bootstrap body does not erase Google Doc revision history
- Control close fences future admissions/claims/begins, not already-consumed execution
- `worker_stop_confirmed=false` until separately observed; no exactly-once external tool claim
- Unknown external creates can leave orphan resources; no blind retry or automatic evidence deletion
- Real single-message pairing, bidirectional actual raw-file access, live parallel latency,
  Mac failure/stop paths and multi-hour endurance are unverified

Use the explicit [upgrade and live acceptance checklist](ROUTER_UPGRADE.zh-CN.md)
before installing or running the candidate against real accounts.
