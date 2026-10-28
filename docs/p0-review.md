# P0: extraction review

Review date: 2026-10-28. This is a source review, not a compatibility certification.
The private upstream's latest remote code and the owner's newer local changes were reviewed separately. The running private checkout was not the development directory. This candidate begins with a fresh Git history and selected tool files; no knowledge directories, device registrations, credentials, runtime state or private Git history are imported.

## What was already implemented

| Mechanism | Latest-source finding | Candidate treatment |
| --- | --- | --- |
| Immutable ledger, canonical projections, rebuildable SQLite | Present | Retain the existing engine and schema |
| Exact resource values and explicit readback | Present; default query output hides exact credential values | Retain; storage policy belongs to each knowledge owner |
| Candidate acceptance/rejection and supersession events | Present | Retain history and strengthen current-answer behavior |
| Hot-only read and capture when cold storage is absent | Present | Retain; do not create a replacement for a missing mounted cold store |
| Incremental source ingestion, durable checkpoints, source identity | Present; local revisions add evidence/schema and retry handling | Retain parser implementation; do not claim real host coverage from fixtures |
| Repository identity/private checks, bounded Git sync, path-limited commits | Present; local revisions add verified rebase rollback and incident suppression | Retain safeguards |
| Source-linked semantic batches, validated proposals, transaction recovery | Present; local revisions add retry isolation and bisection | Retain; expose extension points rather than replacing the engine |
| Actual automation run status | Shared status writer already records attempts, pending, failure and last success | Preserve distinction from scheduler registration |

## Old review findings rechecked

| Finding | Current source review | Required work |
| --- | --- | --- |
| Missing foreground freshness retry | Still applicable | Configurable freshness and one bounded refresh/requery |
| Lifecycle and authority omitted from ranking | Partly addressed by newer local changes | Relevance/source time, expiration, disputes and current/history distinction still need explicit behavior tests |
| Import time can dominate relevance | Still applicable in inherited sort key | Source time and relation-aware selection; test late imports |
| Task-end capture is a verified hook | No such proof | Describe rule-driven capture separately from an observed lifecycle trigger |
| Model budget and usage observations incomplete | Still applicable | Bound a call by remaining time; record real or unavailable usage |
| Oversized first event exceeds batch character budget | Still applicable | Preserve complete source; defer or use a traceable dedicated path |
| Windows same-name Skill protection | Still applicable | Existing installer writes differing SKILL.md content without ownership verification |
| Device/model/timezone values duplicated in automation setup | Still applicable | Replace with owner configuration and capability evidence |
| Planned automation presented as registered or successful | Status mechanisms exist, universal setup does not | Require provider ID, query/readback, trial run and last-success evidence separately |
| Personal storage migration coupled to install | Still applicable to the old Mac installer | Keep external disks, simulators and developer-cache migration out of baseline setup |
| Universal install/upgrade/uninstall | Not present as a product-wide contract | Versioned ownership manifest, conflict protection and data-preserving exit |

## Reuse boundary

The read/write core's dependency closure is `kb.py`, `device_identity.py`, `file_lock.py`, `workspace_lock.py` and `schema/knowledge.sql`. Synchronization adds `git_sync.py`, `hot_sync.py` and `storage_lock.py`. History ingestion adds `ingest_history.py` and `schema/events.sql`. Semantic maintenance adds `semantic_batches.py`, `run_semantic_maintenance.py`, `automation_status.py`, its prompt and configuration.

Personal installers are reference material, not the universal installation entrypoint. Source provenance and selected changes can be reviewed without importing the upstream private object database. Version-sensitive test and host results belong in the acceptance report and compatibility matrix, not this inventory.
