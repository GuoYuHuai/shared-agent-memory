# Experimental candidate acceptance — 2026-10-28

Version: 0.1.0-experimental. This is a reviewable release candidate, not a universal
compatibility certification. No public repository has been created or made visible.

## Executed checks

- Full Python suite: **284 tests run, 283 passed, 1 platform skip**, 93.073 seconds.
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
GitHub-hosted CI is provided but has not run because this candidate has not been published.

## Requirement matrix

“Contract” means deterministic fixtures, not native product or physical-device validation.

| ID | Result and evidence boundary |
| --- | --- |
| A01 | Verified clean local install; no author account or external disk dependency. Remote creation remains an Agent/tool capability step. |
| A02 | Contract: unknown names use verified capability selection. |
| A03 | Contract: read-only retrieval and explicitly unsaved proposals. |
| A04 | Live connector fictional write/readback without CLI; no local installation claimed. |
| A05 | Adapter contract enumerates raw ledger independently of indexes. Live fixture index discovery passed; exhaustive live pending-event enumeration is unverified. |
| A06 | Owned rule file written and preserved. Fresh-session automatic recognition remains unverified. |
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
Doubao Desktop, WorkBuddy, fresh-session persistence, Windows/Linux installation,
two physical devices, native host scheduling, and real semantic-model curation need live
validation. Remote canonical Markdown/candidate-resolution parity, distributed maintenance
leases, automatic oversized-event splitting and schema migration remain explicit follow-up
work. Weekly semantic knowledge review is a pending workflow, not an implemented autonomous
success. No additional model service or subscription was provisioned.

Run `python3 scripts/release_check.py` before packaging. A release owner should review
these limits, the proposed MIT license, and the public repository name before publishing.
