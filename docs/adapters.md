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

## Everyday work

An adapter should help the Agent remember during normal work. Users should not
need to learn record states or ask for a save after every useful exchange.

| Trigger | Expected behavior |
| --- | --- |
| A new session starts a task that needs prior context | Find the configured knowledge root, read its policy, and retrieve relevant context before asking the user to repeat it. Use `ak.py recall --fresh` for the first relevant query in a new session so a recent local freshness receipt does not hide another device's updates. |
| Work produces a reusable fact, preference or decision | Save it with its source and scope under the user's existing policy. Do this when the fact becomes clear or before completing the task; do not wait for a scheduled job or ask for routine confirmation. |
| The user changes an earlier fact | Find the earlier record and use `correct` to preserve the relationship. Ask only when the intended replacement is genuinely ambiguous and affects the work. |
| A save finishes | Verify the local result. `remember` and `correct` attempt bounded private sync when configured; if it remains pending, keep the saved record and let the next opportunity retry. Do not save a duplicate to retry sync. |

Local-only use remains a complete path. An unavailable remote must not erase a
local save or prevent unrelated work. Report an actionable problem briefly;
routine successful memory operations do not need a separate user checklist.

## Instructions and native hooks

The current connection installs persistent instructions. Those instructions ask
the Agent to perform the steps above; the Agent still decides when to call a tool.
This project does not yet ship native lifecycle hooks.

A native adapter may use documented session-start, prompt, task-completion or
equivalent events. Record which events the installed host version actually exposes
and which can deliver context to the model. A visible status message is not proof
that the model received it. If the host has no hooks, use its persistent instruction
or Skill surface and test that path instead.

Keep native adapters small: call the shared commands, respect the existing policy,
and leave unrelated host settings intact. Use short timeouts and idempotent retries;
avoid recursive capture of memory queries or the adapter's own output. A hook
payload is evidence, not a policy change or proof that an observation is true.
Do not make a separate model, full transcript collection, scheduler or resident
service a prerequisite for the ordinary instruction-based path.

## Proving a connection works

Use a separate fictional project and a disposable knowledge directory:

1. In one session, ask for ordinary work that introduces a useful fact, without
   saying “remember,” naming a memory command, or asking for a sync. Confirm that
   the Agent saved it with the correct source.
2. Give an ordinary correction and continue working. Confirm that it replaces the
   current value while the earlier record stays traceable.
3. Start an independent session with no inherited conversation. Ask it to continue
   the task without supplying the answer or a reminder to search. Inspect the tool
   trace and outcome: it must discover the connection, retrieve and use the current
   fact. Reading the answer from a project file alone does not prove memory recall.
4. Repeat with another host or device when that is the claimed support scope. Use
   its actual configured route; two directories on one computer prove only that
   local replica workflow.

Record host/version, OS, instruction or hook scope, date and concise evidence for
capture, correction, retrieval and sync separately. Keep configured, loaded and
observed-working states distinct. If something fails, diagnose the missing tool,
instruction scope, permission or stale copy instead of handing the user a memory
management procedure. Clean up the test connection and fictional data afterward.

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
