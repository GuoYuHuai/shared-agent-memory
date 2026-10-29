# Connect local knowledge to a private GitHub repository

Start locally and connect sync later. Use an existing, installed schema-1 knowledge
directory and a private GitHub repository you have already prepared. This command
does not create a GitHub repository or upload anything.

Ask your Agent:

> Connect this local knowledge directory to my existing private GitHub repository.
> Preserve its facts and settings, verify the repository and history, and tell me
> enable sharing after saves, and verify the first sync.

The optional connection step requires Python, Git, and an authenticated
[GitHub CLI](https://cli.github.com/). The CLI must be able to read the repository's
metadata, and Git must be able to read its refs. The repository's name and an
anonymous “not found” response are not proof that it is private.

From the tool checkout:

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" connect-sync --repository owner/private-knowledge
```

The default origin is the repository's HTTPS URL. An explicit SSH URL is also
accepted:

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" connect-sync --repository owner/private-knowledge --remote-url git@github.com:owner/private-knowledge.git
```

Both forms must identify exactly the selected repository on `github.com`, without
embedded credentials. An existing matching origin is retained. The command will
not replace a different origin, change an earlier repository binding, or adopt
custom push URLs or fetch rules.

## What the result means

| Result | Meaning and next step |
| --- | --- |
| `ready-for-first-push` | The remote had no refs during the check. The local connection is ready; run sync when you want to publish committed knowledge. |
| `ready-for-sync` | The configured branch exists remotely and shares Git history with this local knowledge. Run sync to reconcile and publish changes. |
| `unrelated-remote-history` | Preserve both copies. Clone the existing private knowledge repository into another directory, adopt it with the installer, and review how to import the local facts. Never merge unrelated histories automatically. |
| `remote-branch-missing` | The repository is nonempty but lacks the expected branch. Inspect and adopt its existing knowledge branch; do not treat it as a new empty repository. |
| `privacy-unverified` | No configuration was changed. Verify the repository's private visibility and the GitHub CLI account's access, then retry. |
| `dirty-sync-config` | Review and commit or restore your edits to `config/kb.json`, then retry. Other knowledge edits and unrelated staged files do not prevent connection. |

A successful connection updates `hot_sync.enabled`, `hot_sync.expected_repository`
and the device-local installation receipt together. It creates a local commit
containing only `config/kb.json`; knowledge, rules, policy, task records and
unrelated staged changes are preserved. The receipt records the metadata source,
check time and observed history. It does not claim that another device has read
the knowledge or that any background task has been installed.

The connection itself does not upload knowledge. With `hot_sync.enabled=true`
and the expected repository configured, subsequent `remember` and `correct`
commands attempt synchronization after saving. Your agent should enable this
only within your authorized sharing scope. Use `--offline` to save without network
access for a particular task; turn off `hot_sync.enabled` to suspend sharing.
An explicit sync can also send already saved knowledge:

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" sync
```

In PowerShell, use `py -3 .\scripts\ak.py --root "C:\path\to\my-knowledge" sync`
with the actual path. No model or scheduler is required.

The automatic post-save attempt uses a 30-second deadline for remote requests.
Local writes, rebases and recovery are allowed to finish even if that deadline
passes; the next remote request is deferred. This is not a hard limit on the
total operation time. Local records survive
an offline, busy or failed sync; its result remains pending until a later successful
sync. Your agent handles that retry with the saved records, rather than asking you
to submit them again. An interrupted remote request may still require inspecting
Git's reported state before retrying; never delete a lock or discard changes
without establishing ownership. This does not create a background retry service. `recall --fresh`
checks for new shared information at the start of a session; a pull-only lookup
does not upload pending local writes.

Sync checks the committed data schema and knowledge-library identity before
changing the working copy or pushing. An unknown schema, missing manifest or
different library identity stops the operation and preserves both copies. Check
that the selected repository is correct and use a compatible tool release;
do not edit the schema or identity merely to bypass the check. Different tool
versions can sync when they share a supported data schema. A concurrent remote
change is checked again before retrying.

Connection checks do not fetch into or rebase the user's working repository.
Handled write or commit failures restore the previous configuration, installation
receipt, origin configuration, index and branch position. An uncooperative
concurrent commit is reported for inspection rather than discarded. Abrupt
process termination or power loss is not claimed to be a transactional filesystem
operation: inspect the receipt, configuration and local Git state before retrying.

The automated tests use real disposable local Git repositories and injected
privacy metadata. They verify the connection state machine and recovery behavior;
they do not establish live GitHub authentication, physical-device sync, or native
Agent activation. See [compatibility evidence](compatibility.md).
