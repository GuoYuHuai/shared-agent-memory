# Experimental candidate acceptance — 2026-10-28

Version: 0.2.2-experimental. This is a reviewable release candidate, not a universal
compatibility certification. The independent public repository is
[GuoYuHuai/shared-agent-memory](https://github.com/GuoYuHuai/shared-agent-memory).
Public history contains tool sources, documentation and fictional fixtures only.

## 0.2.2 portability and transport checks

The macOS full suite executed **496 tests: 495 passed and 1 Windows-only skip**
in 170.734 seconds. Node connector contracts and the two-directory local demo
also passed. These checks cover atomic create-only publication, storage capability
probing, legacy device-state preservation, Linux XDG paths, volume identity fallbacks,
Git URL rewrites, injected repository/index variables and existing SSH configuration.
The Linux and Windows hosted matrix runs the full suite; its results must be read
from the corresponding commit, not inferred from this local run.

A real temporary 64 MiB exFAT filesystem on macOS 27.0.1 rejected both hard links
and exclusive rename with ENOTSUP. The installer refused before creating its target,
preserved an existing fixture and removed its probes. The image was unmounted and
removed. macOS exFAT is therefore **not supported by this release**; mocked fallback
tests do not change that result. Linux/Windows exFAT, physical drive handoff and NFS
remain unverified. See [the review and evidence boundaries](portability-review.md).

An independent source review with memories and user configuration disabled identified
an SSH compatibility regression. The fix preserves Git's SSH command and variant
selection; local executable fixtures verify identity arguments and precedence.
The review's suggested Windows legacy-state migration did not apply: the preceding
release already used the same Windows AppData location, as verified against its source.

## Earlier 0.1 checks retained as evidence

- Full Python suite: **317 tests run, 316 passed, 1 platform skip**, 106.908 seconds.
  Command: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -q`.
- `node tests/test_github.mjs`: connector contracts pass, including read-only proposals,
  incomplete enumeration, snapshot churn, schema rejection, readback scope, ID conflict,
  expiry and conflicting corrections. These fixtures do not certify a product connector.
- `python3 examples/local_demo.py`: clean installation in a path containing Chinese and
  spaces, real capture/correction, Git clone into a second directory, current-source
  retrieval with the cold layer absent. All temporary directories removed.
- Live GitHub tools: append/readback of three fictional events, followed by an index.
  An independent Agent used only connector tools, began at the index, read its referenced
  entries at one fixed commit, and selected the corrected green theme with its source.
  This validates the small fixture's indexed discovery, not exhaustive repository listing
  or every named product. Private evidence is deliberately excluded from this package.
- Real macOS launchd health job: registered, executed in a separate background process,
  positive receipt read back, changed intervals rejected, disabled and removal verified.
  See `evals/background-result.json`. No recurring test job remains installed.
- Fixed fictional retrieval set: **18/24 before, 24/24 after**. Source recall in the first
  three results: **20/24 before, 24/24 after**. See `evals/`. No real usage-cost saving
  or reduction in user follow-up questions has been measured.

Host: macOS 27.0.1 arm64; Python 3.9.6; Git 2.54.0 (Apple Git-157); Node 24.18.0.
GitHub-hosted results are recorded in the repository's Actions history; local tests do
not establish hosted CI success.

## Requirement matrix

“Contract” means deterministic fixtures, not native product or physical-device validation.

| ID | Result and evidence boundary |
| --- | --- |
| A01 | Verified clean local install; no author account or external disk dependency. Remote creation remains an Agent/tool capability step. |
| A02 | Contract: unknown names use verified capability selection. |
| A03 | Contract: read-only retrieval and explicitly unsaved proposals. |
| A04 | Live connector fictional write/readback without CLI; no local installation claimed. |
| A05 | Adapter contract enumerates raw ledger independently of indexes. Live fixture index discovery passed; exhaustive live pending-event enumeration is unverified. |
| A06 | Owned rule files are connected and preserved; a new Codex CLI session discovered the fixture and returned its corrected record. Other native hosts and lifecycle hooks remain unverified. |
| A07 | Independent connector Agent read the same fixture/source. Different native products remain unverified. |
| A08 | Real Git clone between two directories; two physical devices remain unverified. |
| A09 | Local and live connector correction/source readback passed. |
| A10 | Contract: scope/device isolation and alias tests. |
| A11 | Contract: expiry, disputed siblings and historical eligibility. |
| A12 | Contract: incomplete/offline coverage does not establish absence. |
| A13 | Local Git fixtures cover offline/recovery; real network-outage deployment unverified. |
| A14 | Contract: independent Git additions preserved; stable-ID create-only path prevents split-path ID races. |
| A15 | Contract: concurrent correction targets expose dispute instead of last-write-wins. |
| A16 | Fixture histories verify incremental checkpoints and repeat ingestion. |
| A17 | Complete oversized events preserved and deferred; zero budget does not advance completion. Automatic long-event splitting remains pending. |
| A18 | Live local demo and contracts: hot reads/capture survive absent cold layer. |
| A19 | Real launchd registration and successful trial are separate receipts; malformed child output cannot claim success. |
| A20 | Contract: repeated install preserves owned rules/tasks and refuses unmanaged or modified content. |
| A21 | Same-schema upgrade/rollback metadata and preservation tested. Future schema writes blocked; schema migration not implemented. |
| A22 | Owned rules removed, knowledge preserved; registered task receipts block uninstall until disabled. Real scheduler teardown verified. |
| A23 | Local read/write and real deterministic health work without a background model. |
| A24 | Explicit source export, new Git history, file-hash manifest and package inspection; no private knowledge/history included. |
| A25 | Fixed questions and source-time tests cover late import/current-answer ordering. |
| A26 | Fixture history/bundle deduplication preserves independent event sources. |
| A27 | Fixture checkpoints, interruption and transaction recovery tested; real long-running host backfill unverified. |
| A28 | Pinned live connector reads; contract churn detection retries once and reports incomplete. |
| A29 | Bounded multi-pass history fixtures pass; no claim of full real-user archive import. |

## Independent audit and repairs

Two independent ephemeral Codex CLI source reviews ran with memories disabled and user
configuration ignored. They did not read personal knowledge. Fixes include repository
schema checks, deterministic ledger ID paths, branch-versus-commit readback receipts,
expired dispute eligibility, reinstall task ownership, positive child execution receipts,
scheduler-operation locking and interval preservation, and proposal revalidation inside
the apply lock. The second review identified a remaining plan/interval issue; it was then
fixed and covered by regression plus a real launchd plan-rejection check. Source review
does not replace runtime testing or establish compatibility beyond the evidence above.

## Remaining release scope

Foreground generic/local and GitHub contracts are usable experimental paths. Claude,
Doubao Desktop, WorkBuddy, desktop-host persistence, Windows installation,
Linux desktop integration, two physical devices, native host scheduling, and real semantic-model curation need live
validation. Remote canonical Markdown/alias parity, distributed maintenance
leases, automatic oversized-event splitting and schema migration remain explicit follow-up
work. Weekly semantic knowledge review is a pending workflow, not an implemented autonomous
success. No additional model service or subscription was provisioned.

Run `python3 scripts/release_check.py` before packaging. Sources use the MIT license;
review these evidence limits before making additional compatibility claims.

## Lightweight and portable defaults

New installations do not collect raw conversations or enable background model work.
Local-only capture, local commit and retrieval run without a GitHub account or remote.
An illustrative empty installation used 51,487 file bytes; ten short fictional records,
local Git and the disposable hot index used 419,315 bytes. No history database was created
and retrieval made zero remote attempts. See `evals/lightweight-storage.json`; these are
fixture measurements, not storage quotas or predictions for arbitrary collections.

Additional source review with memories disabled identified two portable-state issues:
an existing empty mount-point directory could be mistaken for an available disk, and a
copied freshness timestamp could be trusted on another device. The implementation now
checks mounted-volume evidence and binds retrieval freshness to device, knowledge root,
repository, origin, branch and snapshot. Directory-copy/device fixtures cover reattachment;
physical disk and cross-device validation remain explicitly pending.

## 0.2 usability and reliability checks

The final local full run executed **415 tests: 414 passed and 1 platform skip** in
134.740 seconds. Two additional uninstall boundary cases were verified in the
complete 27-case installer suite; all passed. The final source contains 417 Python
tests. Node connector contracts and the updated temporary two-directory
demonstration also passed. Hosted results are available in GitHub Actions for the
corresponding commit.

- The everyday CLI covers initialization, saving, foreground recall, correction,
  instruction connection, local status and optional synchronization. Fixtures include
  spaces and Chinese in paths, external ledger additions, exact CRLF values,
  unsupported schemas, broken connections and missing storage.
- A correction publishes its fact and supersession together in one immutable file.
  Tests cover retries, exact-value requirements, interrupted publication, projection
  recovery, ID conflicts and Python/GitHub container compatibility.
- Real local Git fixtures cover adding sync, empty-remote first push, unrelated
  histories, races, configuration rollback, missing author identity and preservation
  of unrelated staged work. Repository privacy is injected in these fixtures; this
  does not claim a new live GitHub first-push or physical-device trial.
- A new ephemeral Codex CLI session ran with memories disabled and user configuration
  ignored. Its prompt asked for the fictional Atlas preview port without supplying
  a value or knowledge path. It discovered the connected project AGENTS.md, read the
  knowledge files and returned **4311**, the replacement record ID, and source
  `fictional:port-change`. It read the ledger directly; it did not exercise CLI recall
  or prove any other host's behavior. The fixture was removed afterward, and no real
  installation's activation receipt was changed.
- An independent read-only source review used a new context, disabled memories and
  disabled project/global instruction discovery. It identified stale instruction
  blocks after a same-path tool upgrade. Repair and interruption tests cover that
  finding. A follow-up review identified interrupted disconnect and uninstall
  finalization. Both now persist recovery state before changing rules and have
  failure-injection coverage. Integration tests also cover changing tool paths
  while connected and missing-storage rejection.

The English and Chinese READMEs expose the same setup, storage and support boundaries.
The comparison in `docs/choosing.md` uses other projects' official documentation;
no comparative performance, popularity or cost advantage is claimed.

## 0.2.1 reliability checks

Full local suite: **454 tests run, 453 passed, 1 Windows-specific platform skip**
in 147.403 seconds. Node connector contracts also passed. Package checks cover
97 explicitly listed source, documentation and fixture files, plus the manifest.

The [reliability notes](reliability.md) link the upstream sources used to select
reproducible checks. All new fixtures use fictional data in temporary directories.

- Retrieval checks cover exact titles beyond the candidate window, short Chinese
  excerpts, literal punctuation, aliases under a held workspace lock, read-only
  SQLite, explicit fact identities and rejection of unrelated lexical siblings.
- Capture tests inject index and publication-durability failures. Saved records
  remain readable with their IDs; identical retries repair projections without
  replacing the immutable event. The everyday CLI reports the partial outcome.
- Shared Python hot/indexed and JavaScript fixtures check candidate acceptance,
  rejection, future-effective and expired facts, same-identity conflicts, scope
  isolation, correction chains and invalid lifecycle graphs. This verifies the
  ledger contract, not complete remote Markdown retrieval or native host behavior.
- Disposable Git repositories exercise incompatible schemas, different library
  identities, missing or unsafe manifests, concurrent push changes and compatible
  tool-version differences. Both copies survive a rejected sync.
- An independent ephemeral source review disabled memories, user configuration
  and instruction discovery. It identified the accepted-candidate conflict case
  and overly broad sibling expansion. Both received fixes and regression cases.
  The review executed no tests and does not extend the platform coverage.
- Upgrade fixtures reject old lifecycle caches on read and rebuild them on the
  next foreground lookup without changing the source ledger or data schema.

The fixed 24-case retrieval evaluation and the temporary two-directory local demo
also passed. The demo verifies a real local Git clone and correction readback with
the cold layer absent; it does not establish physical-device or online freshness.
