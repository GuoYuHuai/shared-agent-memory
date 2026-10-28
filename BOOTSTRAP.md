# Agent bootstrap protocol

Version: experimental 0.1. This file is the stable entrypoint for any Agent. It does not grant permission beyond the user's request. Read the selected release's protocol, compatibility evidence and target user's policy before mutating data.

## 1. Resolve the installation and target

Pin the tool release or commit. Locate existing installations and the user's knowledge repository before creating a new one. Confirm the target repository identity and private visibility using actual repository metadata; repository names containing “private” are not proof. Do not use the publisher's knowledge repository. If an existing private repository is supplied, attach to it rather than fork the user's memory into an unrelated repository.

Keep `tool_root` and `knowledge_root` distinct in configuration (this experimental installer uses separate directories). Raw history and local state are device-owned. Do not copy a previous user's configuration or require an external disk.

## 2. Probe capabilities

Use [docs/capabilities.md](docs/capabilities.md). Each capability has status, observation time, scope, evidence and blocker. Verify small authorized operations; a tool's name is only a declaration. Unknown Agent names proceed through this protocol. Inspect existing configuration or official host documentation to resolve ordinary paths, encoding, interpreter and integration differences.

Do not equate public repository reading with private access, local execution, persistent instructions or background credentials. An interactive connector session cannot be assumed to authenticate a CLI or scheduled run.

## 3. Choose a usable path

- Local files and execution: run `scripts/install.py --help`, then initialize or connect using explicit knowledge root and repository identity. Confirm the installation ownership manifest before modifying managed files. Refuse same-name foreign configuration replacement.
- Private GitHub read/write without execution: follow [docs/github.md](docs/github.md). Read hot records and ledger through connector tools; append source-linked immutable events with unique IDs and verify by reading them back. No Python, local index or source-device history is required.
- Private read only: read evidence; return a proposal with `saved: false`. Do not claim capture.
- Public read only: explain the missing private access and prepare the concrete connection step; personal knowledge is not connected yet.

Modes are capability combinations, not a mandatory upgrade ladder or brand allowlist.

## 4. Establish foreground behavior

Install the shared retrieval and maintenance protocol in a verified Skill, rule block, project instruction or equivalent supported carrier. Preserve user text and same-name foreign files. Read back the managed entrypoint. A file being present does not prove that a later session loads it: mark that gate pending until observed in a fresh session. If no persistent carrier exists, report session-only use and the shortest supported persistence step.

Before asking for earlier background, search relevant scopes, aliases and source evidence. On a miss or stale local copy, perform the available bounded freshness check and one refresh/requery. Report incomplete coverage or failed sync rather than claiming absence.

Capture a fictional, identifiable test fact or an authorized real fact. Confirm its stable ID, exact content, scope, provenance and saved path. Correct it with a new event and supersession relation; verify the current result and retained old source. Do not promote model inference to user fact.

## 5. Plan and verify automation independently

Separate synchronization, deterministic history capture, semantic curation and knowledge review. Use only actually available scheduling, history and model capabilities. Without a model, deterministic tasks can still work. Without local history access, a cloud Agent may maintain shared knowledge but cannot claim local capture.

Each task needs an owner/device, timezone, cadence, bounded runtime/work budget, query/disable operations and an installation-manifest reference. Use `planned`, `registered`, `trial_succeeded` and `last_success_at` as separate fields. Registration requires provider readback; success requires actual execution evidence. Leave unsupported tasks planned or unavailable. Do not create repeated tasks on reinstall.

Respect sleep/offline conditions; use bounded catch-up and preserve unfinished checkpoints. Only one maintenance writer should consume a batch at a time. See [docs/automation.md](docs/automation.md).

## 6. Verify and report

Report each gate separately:

| Gate | Minimum evidence |
| --- | --- |
| Private target | Repository identity and actual private metadata |
| Local install | Owned files and version manifest, if applicable |
| Persistent entrypoint | Readback plus fresh-session load observation, or pending |
| Read | Stable record ID, scope and source |
| Write | Saved path and immutable event/version readback |
| Correction | New current result, old record still traceable |
| Discoverability | Independent reader finds the appended event even if projection lags |
| Snapshot | Fixed commit or verified generation/content consistency |
| Sync | Local saved, remote committed, queryable and cross-client gates separately |
| Automation | Plan, provider registration, trial and last success separately |
| Cleanup | Remove task-created regenerable scratch; retain user data |

Include tool/schema version, environment, commands or connector operations, actual results, remaining blockers and fallback mode. Contract tests, two directories on one machine, live host installation and two physical devices are different evidence categories. Continue authorized independent work when one gate is unavailable.
