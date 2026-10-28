# Compatibility evidence

Candidate date: 2026-10-28. This matrix separates implementation, contract tests and live host observations. Final commands, versions and pass/fail totals are in [the acceptance report](acceptance.md). Absence of a host environment is a pending validation, not an unsupported brand decision.

| Environment/path | Implemented contract | Evidence boundary |
| --- | --- | --- |
| Generic local files + Python + Git | Local install and shared read/write engine | Installer, daily-command and local-only capture/commit/retrieval tests passed on the development Mac; see the current suite totals in the acceptance report |
| Generic GitHub tools, no local CLI | Append/readback, consistent retrieval and ledger discovery | Node connector contracts passed; three fictional events and an index were written/read through live GitHub tools, and an independent Agent discovered the correction from the index on an isolated private fixture branch. This is not full native-host installation certification |
| Read-only private repository | Source retrieval and unsaved proposal status | Limited by actual listing/read coverage |
| Unknown Agent name | Capability-based route | No product-name rejection; not a native integration certification |
| Codex | Shared Skills/rules and reference runner | A new ephemeral Codex CLI session on macOS discovered the connected project instructions, read the fictional ledger and returned the corrected port with its ID and source. This does not verify desktop lifecycle hooks or recurring tasks |
| Claude | Shared protocol and Skill/rule carrier; inherited history parser | Native installation and fresh-session behavior pending live validation |
| Doubao Desktop | Generic protocol and capability mapping | Product/version/tool-specific validation pending |
| WorkBuddy | Generic protocol and capability mapping | Product/version/tool-specific validation pending |
| Future Agent | Same capability and adapter contract | Contributor must supply real evidence |
| Windows | Portable Python contracts and inherited parsing behavior | Real Windows installation/scheduling pending |
| Linux | Portable Python contracts | The Python and Node suites run on GitHub-hosted Ubuntu; desktop-host integration and native scheduling remain pending |
| macOS launchd deterministic background health | Register, query, execute and disable a managed one-shot task | A real background health run succeeded; test registration was removed afterward. This does not prove a background model or an ongoing installed schedule |
| Removable directory | Mount and volume binding, portable source references and device-local receipts | Directory/device fixtures and a read-only probe of an actual external mount passed; physical disk transfer between computers remains unverified |
| Two physical devices | Shared Git format and sync contract | One machine with two fixture directories is not this evidence |

## Retrieval evaluation checkpoint

The checked-in fictional fixed set has 24 cases. The inherited baseline answered 18 according to the deterministic evaluator; the modified candidate answered 24. These are bounded retrieval checks, not semantic generation quality, a randomized benchmark or a cost benchmark. See `evals/baseline.json`, `evals/current.json` and `evals/retrieval_cases.json`. Additional edge-case tests and final release totals are reported separately.

## Evidence record for contributors

Record Agent name/version, OS/version, tool release, knowledge schema, available tools, permission scope, selected mode, verification date and each tested behavior. Include operation/command, observed result, saved path/version and known limitations. Treat `planned`, `registered`, `trial_succeeded` and `last_success_at` as separate fields.

A useful failure report names the failed capability and still records working capabilities. Do not turn an authentication failure in one environment into a brand-wide incompatibility claim. Do not copy private logs, tokens, knowledge records or host paths into public issues.

## Known release limits

This is an experimental candidate, not a promise of universal automatic installation. Connector capabilities differ; incomplete enumeration must be reported. Native persistence and scheduling may need host-specific adapters. Usage may be unavailable from a model host and must then be marked unknown. No comparative cost savings are claimed. The source review is recorded in [p0-review.md](p0-review.md).
