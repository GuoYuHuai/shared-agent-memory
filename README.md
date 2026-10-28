[English](README.md) · [简体中文](README.zh-CN.md)

# Shared Agent Memory

Shared memory across AI agents and devices. Keep project settings, decisions and preferences in files your agents can read. When you switch tools or computers, the next agent can look up what you have already explained.

For example:

- “The Atlas preview server runs on port 4310.”
- “Keep Atlas progress updates to three short paragraphs.”
- “We chose local storage so the app works offline.”

Each record keeps its source. When something changes, a correction preserves the earlier record and tells the next agent which information to use.

## Get started

Give your agent this repository link and ask it to set things up:

> Help me set up Shared Agent Memory: https://github.com/GuoYuHuai/shared-agent-memory. Read BOOTSTRAP.md and reuse my existing knowledge folder if I have one.

[BOOTSTRAP.md](BOOTSTRAP.md) gives the agent the setup steps: find an existing installation, check the tools it can use, connect a knowledge folder, then test saving, reading and correcting a record.

Once connected, the instructions tell your agent to look up relevant knowledge before working and save useful changes as it goes. You can also ask directly:

> Remember this for Atlas: use a short table for progress updates.

Later, another connected agent can retrieve that preference with its source. Agents need access to the same folder or a synchronized copy to share knowledge.

## Choose where to keep your knowledge

| Storage | Use it for |
| --- | --- |
| Local folder | Working on one computer, with no GitHub account or remote required |
| Your private GitHub repository | Sharing selected knowledge between devices and agents with repository access |
| A folder on a mounted drive | Keeping the knowledge files on storage you choose |

Knowledge folders already set up with this project can be reused. The public project contains the tools; your knowledge stays in your selected location. See [storage and setup](docs/local-storage.md) for details.

The installer checks whether the selected storage can safely save records. On
macOS, exFAT drives currently fail this check; use a supported local or APFS
directory, with private Git sync when you need to share across systems.

Local tools require **Python 3.9+ and Git**. The default setup needs no additional model API or background service and does not import old conversations. History import and scheduled maintenance are optional. Turning off a schedule leaves normal reading and recording available.

## Try the sample locally

With Python 3.9+ and Git installed:

```sh
git clone https://github.com/GuoYuHuai/shared-agent-memory.git
cd shared-agent-memory
python3 examples/local_demo.py
```

The sample saves a fictional fact, corrects it, and reads the correction from a second local copy. It uses temporary folders and removes them afterward. For your own setup, give the agent the prompt above or follow [BOOTSTRAP.md](BOOTSTRAP.md).

## Which agents can use it?

An agent can work with local files or with GitHub tools. The same setup guide works across products; the agent checks which operations it can perform in the current session. An agent that can only read can still retrieve knowledge, but cannot save changes.

The local workflow has been tested on macOS, and the GitHub workflow includes live read/write checks. Other products and systems have varying test coverage. See [compatibility](docs/compatibility.md) before relying on a particular integration.

## Is this a good fit?

If a short Markdown file already handles your preferences, keep using it. Shared Agent Memory becomes more useful when you need several agents or devices to agree on current settings, trace a decision to its source, or stop using an outdated fact after a correction.

It uses text search and explicit corrections. If you want semantic search, a knowledge graph or a ready-made MCP service, see [other approaches](docs/choosing.md).

## More information

- [Connect a local folder to private Git sync](docs/sync.md)
- [Everyday commands](docs/commands.md)
- [How records and corrections work](docs/protocol.md)
- [Using GitHub tools without a local command line](docs/github.md)
- [Optional scheduled tasks](docs/automation.md)
- [Upgrading and uninstalling](docs/upgrading.md)
- [Contributing](CONTRIBUTING.md)

Shared Agent Memory is an early release. Reproducible test results and known limitations are in [the validation report](docs/acceptance.md).

[MIT License](LICENSE).
