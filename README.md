[English](README.md) · [简体中文](README.zh-CN.md)

# Shared Agent Memory

Work with your agent as usual. Let it remember what will matter next time, so a new chat, tool or computer means less explaining from scratch.

Things you mention during work can become shared memory:

- “The Atlas preview server runs on port 4310.”
- “Keep Atlas progress updates to three short paragraphs.”
- “We chose local storage so the app works offline.”

Your agent handles saving, finding and updating these details. You do not need to organize notes or manage records. Sources and earlier versions stay behind the scenes so the agent can tell what still applies.

The current integration uses persistent host instructions. Automatic behavior depends on the agent loading and following them, so setup includes a real fresh-session check. See [compatibility](docs/compatibility.md) for the tested scope.

## Get started

Give your agent this repository link and ask it to set things up:

> Help me set up Shared Agent Memory: https://github.com/GuoYuHuai/shared-agent-memory. Read BOOTSTRAP.md and reuse my existing knowledge folder if I have one.

[BOOTSTRAP.md](BOOTSTRAP.md) guides the agent through finding existing memory, checking tools and permissions, completing the setup it can handle, and testing whether memory is saved and found again. You step in for sign-in, authorization or a necessary choice it cannot resolve.

Once connected, keep working normally:

> Atlas now uses preview port 4311. Continue writing the deployment notes.

The agent should update its memory while completing the task. Next time, “Continue deploying Atlas” should lead it to the new port without asking you again. You can also say “remember this,” “this has changed,” or “don't save this.”

When private sync is enabled, saves and corrections also attempt synchronization; offline changes stay local. Agents need access to the same folder or synchronized copies. A new device still needs an initial connection to your memory repository.

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

The local tools are tested on macOS and on Linux and Windows CI runners. The GitHub workflow includes live read/write checks. Native Agent integrations have varying test coverage. See [compatibility](docs/compatibility.md) before relying on a particular integration.

## Is this a good fit?

If a short Markdown file already handles your preferences, keep using it. Shared Agent Memory aims to reduce the context you repeat across chats, tools and devices: your existing agents maintain the same memory, and the next agent can find the current settings when something changes.

It uses lightweight files and text search, without a separate model or database service. If you want ready-made host hooks, semantic search or an MCP service, see [other approaches](docs/choosing.md). Choose what actually reduces repeated explanations in your environment.

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
