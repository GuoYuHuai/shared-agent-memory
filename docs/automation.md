# Automation and measurement

Every job has separate planning, registration, trial, and last-success fields in
`<knowledge-root>/.ai-knowledge/automation/<job>.json`. These device-local receipts
must not be synchronized as knowledge. An execution ID, UTC start/end, elapsed
seconds, retry count, outcome, and available event counts are recorded. Token usage
is `unknown` when the platform did not return it; deterministic health uses
`not-applicable` and zero model calls. Null is not zero cost. No savings claim is made.

## Deterministic reference

From the selected tool release, with an already installed knowledge root:

```sh
python3 scripts/automation.py --root '/path/to/private knowledge' plan health
python3 scripts/automation.py --root '/path/to/private knowledge' run health
python3 scripts/automation.py --root '/path/to/private knowledge' status health
```

The health job validates hot JSON syntax and policy presence without a model or
cold storage. It is a narrow health check, not semantic correctness certification.
A manual run changes trial evidence only; registration remains `not-registered`.

On macOS, after checking user authorization for recurring execution:

```sh
python3 scripts/automation.py --root '/path/to/private knowledge' register-launchd health --interval-seconds 1800
python3 scripts/automation.py --root '/path/to/private knowledge' status health
python3 scripts/automation.py --root '/path/to/private knowledge' disable health
```

Registration uses a root-derived unique identity, stores its owned plist next to
its receipt, queries launchd after bootstrap, and does not write login startup
files. Disable queries again after bootout. A matching unowned scheduler identity
is refused. Repeating registration does not create a second task. The reference
is session-bound launchd; reboot/login persistence is not claimed. The installer
must refuse removal while a receipt remains registered. The computer must be awake.

On 2026-10-28 a temporary launchd health task on macOS 27.0.1 arm64 was actually
registered, executed by a separate background Python process, read back as succeeded,
and disabled with absence verified. It validated one fictional JSON entry.
This does not establish Windows scheduling, native Codex automation, or model execution.

## Four work classes

- `sync`: existing repository identity, path restriction, locking, no-force sync machinery.
  The automation entry performs pull-only; use `hot_sync.py` to commit and push authorized
  canonical changes. Set `AI_KNOWLEDGE_ROOT` to the private knowledge root.
- `capture`: opt-in `history_capture.enabled`, `source`, and `maximum_lines` in that root's
  `config/kb.json`. Codex/Claude parsers are reference formats, not an Agent admission
  whitelist. Unknown source formats require a new parser or foreground capture.
- `curate`: opt-in `semantic_maintenance.enabled`; a configured runner must really be
  callable. The inherited Codex runner reads a source-bound batch and emits a proposal.
  A host checks provenance and stages only allowed paths in a detached transaction.
- `review`: reports pending foreground knowledge review. This experiment does not claim
  autonomous weekly semantic knowledge review. The inherited report-only code audit is
  separate and must not be presented as knowledge review.

`capture` and `curate` remain pending when not enabled. Frontend capture/retrieval and
health continue without either. No runner is installed or purchased by default.
An external scheduler can call the same finite jobs; it must supply its own query,
disable and actual execution evidence. Native host scheduler adapters are documented,
not certified in this release. Connector authentication is not passed to CLI runners.

## Bounded work and recovery

`ingest_history.py --scan --max-lines 2000` stores a durable checkpoint only after
committed history data. Repeated calls resume complete source lines, including across
multi-round initial backfill. A partial final line is retained for the next run. Unsupported
records stay quarantined; bounded scans do not trigger unbounded quarantine replays.
One indivisible JSON line can exceed a byte budget; no byte or strict wall-time bound
is claimed for source parsing. Raw histories remain at source and are not cache cleanup.

Semantic settings: `maximum_events_per_batch`, `maximum_characters_per_batch`,
`maximum_batches_per_run`, `maximum_model_calls`, `maximum_run_seconds`. Runner timeout
uses remaining run budget; the outer job timeout bounds the task. Setup/indexing can
also consume time, so budget telemetry describes actual elapsed time rather than a
promise of exact scheduling latency. Interrupted or budget-exhausted work is not complete.

An oversized first event is preserved whole in a separately flagged batch. It is not
silently truncated or marked processed. Default execution returns
`oversized-event-needs-budget`. Inspect the required size and use a deliberately configured
large-event run (`allow_oversized_events`) or a future staged reader. This experiment does
not yet automatically split oversized semantic events; that backlog remains visible.
Existing retries, source/context fingerprints, event deduplication, and transaction recovery
are retained. Scheduling across several independent knowledge clones still needs an
external single-owner policy for weekly review; local locks are not distributed leases.
