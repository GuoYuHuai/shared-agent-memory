# Everyday commands

Your Agent can run these for you. Use a separate folder for your knowledge and
read its POLICY.md before saving information. Commands below run from the tool
checkout; replace the example paths with your own.

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" init
python3 scripts/ak.py --root "/path/to/my-knowledge" remember "Atlas preview uses port 4310" --id atlas-preview --scope project:atlas --source user:setup
python3 scripts/ak.py --root "/path/to/my-knowledge" recall "preview port" --scope project:atlas
python3 scripts/ak.py --root "/path/to/my-knowledge" correct atlas-preview "Atlas preview uses port 4311" --source user:correction
```

The correction keeps the earlier record and its source, but removes it from
current answers. Repeating the same correction is safe. Record IDs are printed
after saving; use the current record's ID for a later correction.

If saving succeeds but an index update fails, the result still shows the saved
ID and a recovery action. Keep that ID: retry the same `remember` command with
`--id ID` after the storage issue is resolved. This repairs the index without
creating another fact. In JSON output, check `saved` and `readback` separately
from `ok`; a follow-up failure returns a nonzero exit code.

Use `--authority agent-inferred` when recording an unconfirmed inference. It
remains a candidate, not a current fact. Use `--value-file FILE` to preserve an
exact configuration value, including line endings, without putting it in the
command arguments. A correction to a record with an exact value requires its
new value file. Authorized retrieval can include values with `--include-values`.

## Connect your Agent

Choose a project or user instruction file that your Agent actually loads. For
example, a Codex project may use AGENTS.md and a Claude Code project CLAUDE.md.
Other hosts can use their documented instruction file or equivalent setup.

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" connect --instructions "/path/to/project/AGENTS.md"
python3 scripts/ak.py --root "/path/to/my-knowledge" status
```

The connection adds a managed block with the knowledge location and everyday
commands. Existing project text stays in place. Open a new Agent chat in that
project and ask about a saved fictional fact. Check that it retrieves the
record and its source; writing the block alone does not prove the host loaded it.

To remove the connection:

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" disconnect --instructions "/path/to/project/AGENTS.md"
```

## Sharing during ordinary work

Local use needs no remote. After you enable your private repository with
[the sync guide](sync.md), `remember` and `correct` attempt one synchronization
after verifying the local save. This attempt limits remote waiting to its
30-second deadline while allowing local writes and Git recovery to finish; it
is not a hard limit on total operation time and does not register a background
task. Use `--offline` on either command when network
use is not authorized or you want to save only locally.

An interrupted or failed sync leaves the saved record in place. JSON output
keeps `saved`, `readback`, `synced` and `sync_pending` separate. A follow-up failure
returns a nonzero exit code, so inspect the saved ID before retrying; do not create
another record to retry sharing. Your agent can retry with:

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" sync
```

Without a remote this commits saved knowledge locally. With a configured
private remote it also attempts synchronization and reports the result. A
pending or failed sync leaves the saved records available locally. Another
device must synchronize its own copy before it can read those changes.

`recall` refreshes the local search index and checks configured remote freshness.
Use `recall QUERY --fresh` for the first relevant query in a new session. It checks
the configured remote even when a cached result already matches, so a correction
from another device can be found without waiting for the freshness interval.
Use `--offline` when you deliberately want only the available local snapshot;
it takes precedence over `--fresh`.
`status` shows recorded state without contacting the remote. Lower-level
`kb.py search` is a read-only snapshot query and does not refresh a stale index.

For structured output, put `--json` before the command:

```sh
python3 scripts/ak.py --root "/path/to/my-knowledge" --json recall "preview port" --scope project:atlas
```

Run `python3 scripts/ak.py --help` for the full list. On Windows, use an available
Python 3 interpreter, such as `py -3`, and the actual Windows paths.
