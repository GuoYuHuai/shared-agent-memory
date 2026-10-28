# Ownership, upgrade and exit

Tools live in their own versioned checkout. Knowledge is a separate directory selected
with `--root`; installed rules record both paths. No upward-directory guessing is needed.
The installer creates a new data root or updates its own `.ai-knowledge/install.json`
installation. It attaches a recognized schema-1 clone with a matching origin and shared identity without replacing policy or data; it refuses other unmanaged nonempty directories. Existing personal installations
need a reviewed migration, not an overwrite. Private visibility must be checked by the
Agent using authenticated GitHub metadata before first remote write; an owner/name flag
alone is an intent, not authentication evidence.

Repeat installation refreshes only the exactly hash-matching owned rule block and its
installation manifest. Text outside the block, POLICY.md, config and all knowledge remain.
A changed owned block, mismatched repository, or unsupported schema stops the operation.
Install manifests are backed up locally. The current release performs no schema migration.

To upgrade, obtain a reviewed release in a separate tool directory, inspect its manifest
and run that version's install.py against the existing root and repository. Re-run read,
capture, correction and health checks. To roll back schema-1 tool-only upgrades, reinstall
the previous reviewed tool directory; verify the same checks. A future schema migration
requires a locked backup and explicit restore/compatibility procedure; merely switching
code is not a supported data rollback. Do not point an older writer at a newer schema.

Disable and verify every managed automation receipt first. Then:
`python3 scripts/install.py --root /your/knowledge --uninstall`.
Uninstall removes only the owned rule block and active install marker. It leaves knowledge,
raw history, user policy and backups. Keep the tool checkout while another installation
references it. Uninstalling a runner does not remove access to files or GitHub tools.

Interrupted first installs currently fail closed as unmanaged partial directories. Preserve
their contents and inspect before retrying into a new empty root. No automatic destructive
cleanup or migration is attempted. Multi-file install is not advertised as transactional.
