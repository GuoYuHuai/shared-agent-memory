# Ownership, upgrade and exit

Tools live in their own versioned checkout. Knowledge is a separate directory selected
with `--root`; installed rules record both paths. The directory can be local or on
selected removable storage. Local tools require Python and Git; GitHub and a remote
are optional. No upward-directory guessing is needed.

## Existing and new installations

The installer creates a new data root or updates its own `.ai-knowledge/install.json`
installation. It can attach a recognized schema-1 knowledge directory with a shared
knowledge identity, policy and Git history without replacing its data. If a repository
is configured, that identity and the origin must match. An offline/local installation
without a configured repository can remain local. Other unmanaged nonempty directories
require inspection or a reviewed migration; they are not overwritten.

For a private GitHub target, the Agent checks authenticated repository metadata before
first remote write. An owner/name flag is intent, not authentication evidence. Pure
local use has no corresponding GitHub-identity gate.

Removable mode requires an actual mounted non-system volume and records its identity.
It rejects an empty leftover mount directory, missing mount, or an existing knowledge
installation bound to a different volume before writing. Stable volume identifiers are
used when the platform supplies them; a runtime-only identity can require revalidation
after remount. This does not prove physical removability or a two-device handoff.
The mode does not configure mounts, transfer files or create a GitHub remote. Explicit
local mode remains available for a separately reviewed ordinary-directory migration.

Repeat installation refreshes only the exactly hash-matching owned rule block and its
installation manifest. Text outside the block, POLICY.md, config and all knowledge remain.
A changed owned block, mismatched configured repository, or unsupported schema stops
the operation. Install manifests are backed up locally. Default new installations are
lightweight; reinstall does not reset a user's existing history or retention settings.
The current release performs no schema migration.

To add sync to a local installation, use [connect-sync](sync.md). Do not reinstall
with a different repository flag: attaching a new remote is a separate operation.

## Upgrade and rollback

Before moving to a different tool directory, disconnect instruction files with the
current tool version and stop its managed tasks. Keep the old checkout until these
steps are verified. Obtain the new release and inspect its manifest. Run
that version's installer against the existing knowledge root, preserving the applicable
mode and optional repository. An omitted repository on a recognized installation retains
its existing repository binding; omission does not silently turn off configured sync.
For example, an existing pure-local installation uses:

```sh
python3 scripts/install.py --root "/path/to/my-knowledge" --mode local
```

An existing removable installation uses `--mode removable` with its actual mounted root.
Changing repository identity or moving data is a separate, reviewed operation, not a
side effect of choosing a mode. Re-run read, capture, correction and health checks.
Check remote sync only when configured. Reconnect each instruction file using the new
tool version, then verify retrieval from a new Agent chat. A same-path update can
refresh an unchanged owned block by running connect again; edited blocks require
reconciliation before replacement.

For schema-1 tool-only rollback, reinstall the previous reviewed tool directory and verify
the same checks. A future schema migration requires a locked backup and an explicit
restore/compatibility procedure; merely switching code is not a data rollback. Do not
point an older writer at a newer schema.

## Stop scheduling or uninstall

Disconnect instruction files with `ak.py --root ROOT disconnect --instructions FILE`
before uninstalling. The command preserves the surrounding project rules; modified
or unverified blocks require reconciliation. Uninstall refuses outstanding connections.

Scheduled maintenance is optional. Disable and verify any managed tasks that actually
exist; an installation with no tasks needs no scheduler setup before uninstalling.

```sh
python3 scripts/install.py --root "/path/to/my-knowledge" --uninstall
```

Uninstall removes only the owned rule block and active install marker. It leaves knowledge,
raw history, user policy and backups. Keep the tool checkout while another installation
references it. Removing a runner does not remove access to knowledge files or configured
GitHub tools. Pausing scheduled jobs alone leaves normal foreground use available.

Interrupted first installs currently fail closed as unmanaged partial directories. Preserve
their contents and inspect before retrying into a new empty root. No automatic destructive
cleanup or migration is attempted. Multi-file install is not advertised as transactional.

Interrupted disconnect and uninstall operations keep local recovery receipts.
Retry the same command to finish. If the file changed after the planned removal,
the operation preserves those changes and reports what needs reconciliation.
Complete a pending uninstall before reinstalling. Repeated completed uninstall is
safe only for the same verified installation owner.
