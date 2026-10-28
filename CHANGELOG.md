# Changelog

## 0.2.2 experimental candidate — 2026-10-28

- Rename the public project to Shared Agent Memory. Existing knowledge formats, configuration names and connected directories remain compatible.
- Check storage publication before installation. Use a native exclusive rename when hard links are unavailable; never replace an existing immutable record. macOS exFAT remains unsupported and now fails before creating an installation.
- Check effective fetch and push destinations and isolate Git routing environment variables while retaining normal authentication and proxy configuration.
- Use XDG state paths for new Linux installations, retain existing device identities, and share mount discovery between installation and cold-storage validation.
- Add Linux filesystem UUID and Windows volume GUID bindings, with explicit runtime-only fallback when a persistent identity is unavailable.
- Add Windows to hosted checks, clarify history-parser boundaries, and fix misplaced docstrings and unused code.
- Preserve user instruction bytes during upgrades and removal; accept LF/CRLF conversion within a verified managed block without accepting other edits.

## 0.2.1 experimental candidate — 2026-10-28

- Keep exact-title matches in search results and show matching excerpts for short queries. Treat `%` and `_` as literal search text.
- Preserve aliases, project scopes and conflict handling when an index is busy or unavailable.
- Distinguish a saved record from a failed index update; retrying the same ID repairs the index without duplicating the record.
- Align GitHub ledger reads with local candidate decisions, effective dates and conflict rules.
- Check knowledge identity and data format before Git synchronization, including concurrent push recovery.
- Document the upstream design lessons and regression cases in [reliability notes](docs/reliability.md). Installation and lightweight defaults are unchanged.

## 0.2.0 experimental candidate — 2026-10-28

- Add complete English and Chinese READMEs with language links, a shorter setup guide and an honest comparison of other memory workflows.
- Add everyday commands for saving, recalling and correcting records, plus instruction-file connection and status.
- Publish a correction and its replacement relationship together; GitHub readers now accept the same multi-event files.
- Connect an existing local knowledge folder to an empty or related private GitHub repository without replacing its history. Support the first normal push to an empty remote.
- Preserve Git settings and unrelated staged work; local saving also works without a preconfigured Git author.
- Refresh foreground retrieval, exclude lifecycle bookkeeping from current facts, and report broken instruction connections.
- Preserve project instructions during connection, upgrade and disconnection. Verify a new Codex CLI session against a fictional corrected record; other hosts still need their own live checks.


## 0.1.0 experimental candidate — 2026-10-28

- Extract selected reusable knowledge tooling into a new clean-history repository.
- Add a universal capability-based bootstrap and explicit local/GitHub-tool routes.
- Retain immutable events, provenance, lifecycle relationships, rebuildable indexes, cold-storage fallback, bounded Git behavior and validated semantic proposals.
- Add public installation ownership, retrieval/freshness and automation-budget work; consult the acceptance report for what was actually verified in this candidate.
- Add fictional demonstrations, contribution templates and compatibility evidence rules.
- Default to lightweight knowledge with local-only, optional private GitHub sync and removable-directory setup.
- Preserve device identity and isolate scheduler receipts when moving between devices; history retrieval is explicitly opt-in for new installations.
- Support local commits without a remote and keep proactive capture, direct user requests and optional schedules equally usable.

This experimental release has an explicit support boundary. Native hosts and physical devices require their own evidence. It does not migrate or change visibility of an existing private repository.
