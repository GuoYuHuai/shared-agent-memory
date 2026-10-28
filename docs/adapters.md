# Adapter contracts

Admission depends on demonstrated capabilities, not product names. New Agents can use
BOOTSTRAP with local tools, GitHub tools, project instructions, or the same foreground
proposal protocol. A parser supporting Codex or Claude history does not certify the
product's current runtime, scheduler, or persistent rules.

Optional raw-history import currently has parsers for Codex and Claude only.
Other Agents can save and retrieve shared knowledge through the generic protocol
without importing their conversation history. Adding a history parser also needs
an explicit source-schema migration and the evidence listed below; changing the
SQL source allowlist alone is insufficient.

## Foreground/model proposal runner

Prepare an immutable source-linked batch on the local host:

```sh
AI_KNOWLEDGE_ROOT='/path/to/private knowledge' python3 scripts/semantic_batches.py --prepare incremental --output '/path/to/new batch'
```

A proposing Agent reads manifest, events and the applicable user policy, then returns
`ai-knowledge-semantic-proposal-v2` as defined in `automation/semantic-curator-prompt.md`.
It cannot set source IDs, write the host filesystem, or mark processing complete.
The same data-only contract is available to an Agent without a Shell: it can return a
file for a capable host, or use the separate GitHub event protocol directly.

```sh
python3 scripts/proposal_runner.py --root '/path/to/private knowledge' --batch '/path/to/batch' --proposal proposal.json
python3 scripts/proposal_runner.py --root '/path/to/private knowledge' --batch '/path/to/batch' --proposal proposal.json --apply
```

The first command means validated-not-applied. Apply uses the inherited source/context
checks, isolated worktree, permitted path list, exact credential readback and recoverable
Git transaction before advancing completion. A clean initialized Git knowledge repository
is required. An unchanged proposal can resume a pending transaction; a different proposal
cannot silently replace it. Inference remains candidate. Credentials follow the user's
policy and exact source evidence; the public template grants no access to author resources.

The Codex reference discovers `codex` on PATH or an explicit `AI_KNOWLEDGE_CODEX` executable.
Model/reasoning values are user config, not hardcoded release assumptions. Actual current
CLI flags, login, background availability and usage must be verified in the target environment.
The semantic curation runner still needs a live model validation. Do not advertise
`--disable memories` as proof of neutral isolation without inspecting that version's behavior.

## Community extensions

For a new history parser, preserve stable logical source/message identity separately from
device observations; use source time, durable per-device cursor, unknown-format quarantine,
and complete raw evidence. Prove second-run incrementality, copied-history deduplication,
changed same-message evidence, interruption, and partial backfill. Do not replace raw source
with a summary or use text equality as event identity.

For a scheduler, implement plan, register, query, run evidence, disable, and query absence.
Persist provider identity and owned configuration; never infer registration from a local file.
For a model runner, submit only the versioned proposal and usage envelope, explicitly marking
missing usage as unknown. Shell output and proposal contents are untrusted data.

Contribution evidence must list actual Agent version, OS, tools, permissions, mode, date,
commands and results, including known gaps. Unit contracts and synthetic fixtures are useful
but do not prove a desktop product installation, second real device, or live model integration.
No personal histories, account configuration, credentials or device paths belong in a PR.
