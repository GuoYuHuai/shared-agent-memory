# Portability review

Rechecked on 2026-10-28 against 0.2.1, with changes in 0.2.2. An external review
of an earlier candidate provided useful test cases. Each finding was checked
against current code before changing it.

| Finding | Current result |
| --- | --- |
| Empty private repositories cannot receive a first sync | Already fixed in 0.2.0. Disposable Git fixtures exercise empty remotes, concurrent first pushes and unrelated histories. |
| Hard-link publication fails on exFAT | Confirmed on a real mounted macOS exFAT disk image. Hard links and exclusive rename both returned unsupported. Installation now checks publication capabilities before creating knowledge files. macOS exFAT remains unsupported. |
| Git address rewriting can bypass destination checks | Fetch URL rewriting was already checked. Push-only rewriting, extra push URLs and inherited Git routing environment variables needed additional protection and are now covered. |
| Linux uses a macOS state directory | New Linux state uses XDG locations; existing state stays in place to preserve device identity. |
| External cold storage assumes `/Volumes` | Installation and cold-storage checks now share discovery of actual non-system mounts. Identity-marker checks remain required. |
| Non-macOS volume identities are only device numbers | Linux filesystem UUID and Windows volume GUID are used when available. Fallback is explicitly marked runtime-only. These identifiers are not promised to match across operating systems. |
| All tests ran only on macOS | Ubuntu hosted runs already existed. Windows has been added to the workflow; see Actions and the acceptance report for results rather than treating the workflow file as proof. |
| Two misplaced docstrings and unused variables/imports | Corrected. The earlier capture catch-and-reraise had already been replaced by explicit partial-save recovery in 0.2.1. |
| Windows instruction files differ from their installation receipts | Found in the hosted Windows run. Installation now writes the exact hashed UTF-8 block, preserves surrounding user bytes and accepts line-ending conversion alone when checking ownership. |
| GitHub installation mode duplicates local-with-repository | Kept as a compatibility alias and explained in CLI help. Normal setup uses local mode plus an optional repository. |

## Immutable writes on different filesystems

The small `atomic_publish` module first tries a hard link. If unavailable, it uses
[macOS exclusive rename](https://developer.apple.com/documentation/foundation/urlresourcevalues/volumesupportsexclusiverenaming),
[Linux `renameat2` with `RENAME_NOREPLACE`](https://man7.org/linux/man-pages/man2/renameat2.2.html),
or [Windows rename](https://docs.python.org/3/library/os.html#os.rename).
The destination must never be replaced. Checking for a file and then calling
replace would leave a race between competing writers and is deliberately excluded.

A temporary-directory probe verifies complete readback and refusal to overwrite.
It checks the actual target filesystem, not its label. If no suitable operation
exists, installation stops with a supported-local-storage/private-Git alternative.
The probe does not establish power-loss durability, removable hardware reliability
or shared network-filesystem locking.

The native macOS 27.0.1 test used a disposable 64 MiB exFAT image: link and exclusive
rename both returned `ENOTSUP`; the new installer refused before creating its
target directory. An existing fictional file was unchanged. The volume was normally
ejected and the image removed. This is a mounted-filesystem test, not a physical
disk handoff. Linux and Windows exFAT devices still need their own checks.

## Git routing without losing normal authentication

[Git `remote get-url`](https://git-scm.com/docs/git-remote#Documentation/git-remote.txt-emget-urlem)
expands rewrite rules. We check all effective fetch and push destinations, including
the first connection before contacting a remote. Repository/worktree/index and
command-scoped configuration injection cannot override the selected knowledge root.

Normal credentials, proxies and legitimate HTTPS-to-SSH rewrites remain usable.
Network Git operations permit HTTPS and SSH; the internal disposable history probe
has a separate file-only path. These controls do not isolate a hostile host: local
credential helpers and SSH configuration remain trusted, and another process must
not edit Git configuration concurrently with synchronization.

## Device state and remaining scope

Linux follows the [XDG state specification](https://specifications.freedesktop.org/basedir/latest/).
Existing legacy state is retained rather than silently assigning a new device ID.
Stable volume lookup uses [findmnt](https://man7.org/linux/man-pages/man8/findmnt.8.html)
on Linux and the [volume GUID API](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumenameforvolumemountpointw)
on Windows. An unavailable stable identifier is reported as runtime-only.

- Generic capture and retrieval remain Agent-independent. Optional raw-history
  parsers currently cover Codex and Claude; see [adapter contracts](adapters.md).
- The history SQLite bootstrap version 0 is intentionally migrated by
  `initialize_database`. This is separate from knowledge schema 1. A future knowledge
  schema change needs its own migration, interruption and rollback evidence; none is
  claimed by this release.
- Large modules remain a maintenance concern. New filesystem-specific logic is
  isolated in its own module; broader extraction should preserve tested behavior
  and happen in reviewable steps.
- NFS/SMB concurrent writers are unverified. Adding a second lock family does not
  establish compatibility: [lock behavior depends on the OS, server and mount options](https://man7.org/linux/man-pages/man2/flock.2.html).
  No distributed-lock guarantee is made.

Relevant regression suites: `test_atomic_publish`, `test_portable_identity`,
`test_git_transport`, `test_git_sync`, `test_connect_sync` and `test_install_public`.
