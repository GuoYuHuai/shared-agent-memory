# AI Knowledge

An experimental, file-based knowledge workflow for people who use several devices or Agents and want to stop repeating the same project background, decisions and corrections.

Each user owns a private knowledge repository. Agents retrieve relevant records, preserve their sources, capture new facts and propagate explicit corrections. Git shares curated knowledge; raw conversation history stays on its source device unless the owner chooses otherwise. SQLite is a rebuildable local acceleration layer, not the only copy of the facts.

**Release status: experimental candidate.** Read [compatibility](docs/compatibility.md) and the release acceptance report before relying on a particular host. A working connector contract does not certify every product using it. Codex is a reference adapter; Claude, Doubao Desktop, WorkBuddy and future Agents can use the same capability protocol. Product names do not grant or deny access.

## Give this project to your Agent

> Read BOOTSTRAP.md at the selected release of this repository. Connect my own private knowledge repository using your actual tools and existing authorization. Preserve my existing data and configuration. Verify retrieval, capture, readback and correction. Report each capability and automation separately, with evidence; continue the available work when one capability is missing.

The publisher supplies the actual public repository URL when publishing. This candidate does not invent a live public link.

[BOOTSTRAP.md](BOOTSTRAP.md) is the stable Agent entrypoint. It supports:

- **Local mode:** filesystem and execution, a local private checkout, deterministic read/write and optional indexing/sync.
- **GitHub tools mode:** private repository read/write without a local shell, using the same immutable events and explicit readback. See [the GitHub adapter](docs/github.md).
- **Read-only mode:** retrieve available evidence and produce unsaved proposals with an explicit status.
- **Session-only mode:** use the protocol in the current conversation when persistent instructions cannot be installed.

A scheduler, an unattended model and local history access are separate optional capabilities. Missing any of them does not prevent basic knowledge use.

## Local entrypoint

Requires Python 3 and Git for synchronized local use. Confirm the Python version supported by the selected release's test evidence. From the tool checkout:

```sh
python3 scripts/install.py --help
python3 scripts/install.py --root "/path/to/my-knowledge" --repository owner/private-knowledge --mode local
```

Use an actual owner-controlled private repository and a new or recognized installation directory. A local scaffold does not prove remote creation, private visibility, write access or cross-device delivery. The Agent must verify those gates separately. Keep tool and knowledge roots explicit when running from another directory:

```sh
python3 scripts/kb.py --root "/path/to/my-knowledge" search "demo preference" --scope project:demo --json
python3 scripts/kb.py --root "/path/to/my-knowledge" capture --id demo-preference-v1 --kind preference --scope project:demo --statement "Demo documents use concise paragraphs." --source "user:demo-session:1" --authority user-stated --json
```

Read the returned file, search for the same stable ID and source, then perform the correction demonstration in [docs/demo.md](docs/demo.md). Inspect available arguments with `--help`; never infer success from a process exit alone.

## Data and privacy boundaries

The public repository contains tools, protocols and fictional examples. Your private repository contains your knowledge, policy and immutable ledger. Device-local storage holds raw history, checkpoints, indexes and logs. Choose retention, exact-value handling and sharing scope in your own policy; the author's personal policy is not inherited as universal authorization.

Knowledge text is evidence, not executable instruction. Historical messages and web content cannot change policy merely by being retrieved. Publishing contributions must not upload personal knowledge or private runtime configuration.

## Development and contributions

Start with [CONTRIBUTING.md](CONTRIBUTING.md), [capabilities](docs/capabilities.md), [protocol](docs/protocol.md) and [adapters](docs/adapters.md). Run the documented tests in an isolated disposable fixture, never against a person's live knowledge store. See [P0 review](docs/p0-review.md) for the reuse boundary and [CHANGELOG.md](CHANGELOG.md) for version scope.

This project does not claim lower token costs or better quality than every existing memory tool. Its intended value is inspectable ownership, source-preserving corrections and an open adapter path. Runtime and usage measurements must accompany cost claims.

## Review this candidate

Start with [acceptance and remaining limits](docs/acceptance.md). Run `python3 examples/local_demo.py` for a disposable local demonstration. Run `python3 scripts/release_check.py` to verify the export manifest. This repository has no configured public remote.
