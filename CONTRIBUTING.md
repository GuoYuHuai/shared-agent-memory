# Contributing

Contribute an adapter or a concrete behavior fix without duplicating the knowledge engine. Unknown products are welcome: show which capabilities and permissions exist, how they were probed, and what worked.

1. Read `BOOTSTRAP.md`, `docs/protocol.md` and `docs/capabilities.md`.
2. Work in a fresh development checkout with fictional fixtures. Keep personal knowledge, device registration, raw history, credentials and private Git history outside it.
3. For an adapter, map repository reads/writes, version checks, rule persistence, history, scheduling and runner operations individually. Implement unsupported operations as accurate unavailable states.
4. Add behavior tests for success, insufficient permission, version races, interrupted recovery and idempotent repeat operations relevant to the change. Do not invent a live host result from a mock.
5. Run the focused tests and the release test command in the selected release's documentation. Tests must use disposable temporary roots and clean task-created state on success and failure.
6. Record Agent/version, OS, release/schema, permissions, date, commands/operations and evidence category in `docs/compatibility.md` or the PR.
7. Submit only with the contributor's authorization. Automated work may prepare a PR draft locally without publishing it.

For Python behavior tests, the baseline command is:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Run any Node adapter tests documented in `docs/github.md` as well. CI status is not evidence of native installation or physical-device acceptance.

## Contracts and licensing

Keep stable IDs, schema compatibility, provenance, current/history semantics and source preservation. Version breaking changes and document migration/rollback. Preserve user-owned files during installation and upgrades; fail clearly on ownership conflicts. Keep task registration separate from actual successful execution.

New dependencies need documented purpose, license and platform impact. Do not copy third-party source or documentation without attribution and a compatible license. Contributions are offered under this repository's MIT license unless explicitly documented otherwise.

## Reporting problems

Use the bug template. Supply a minimal fictional reproduction and the smallest relevant diagnostic excerpt. Exact private records may remain in a private report selected by the owner; public contributions must not contain them. Report incorrect “nothing found” or “all complete” claims as correctness bugs, even if the underlying command exited successfully.
