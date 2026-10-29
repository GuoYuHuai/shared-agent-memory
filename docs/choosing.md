# Choosing a memory workflow

A short Markdown file is often enough. If you work in one project, have a few
stable preferences, and can ensure your Agent reads the file, start there. Add
sources and dated corrections when they help. A larger memory system should earn
its setup and maintenance cost by solving a problem you actually encounter.

Shared Agent Memory aims to make ordinary work carry over: an Agent saves useful
preferences, settings and decisions as you work, then another session can find
them without asking you to explain again. The local tools use Python and Git;
private Git sync is optional. Everyday use does not need a separate model service
or scheduled task.

## When automatic capture matters most

Start with the Agent you actually use. A memory tool with working integration in
that host can be more useful than one with a longer feature list. These projects
connect memory to concrete points in a conversation:

| Option | How it enters the workflow | What to check |
| --- | --- | --- |
| [MemSearch](https://github.com/zilliztech/memsearch) | Its Claude Code plugin injects recent notes at session start, offers a recall hint with prompts, and summarizes completed turns into Markdown. Other hosts have their own adapters. | The prompt hint makes recall available; it does not mean a search ran. Check your adapter's capture model, embedding setup and storage. [Hook configuration](https://github.com/zilliztech/memsearch/blob/main/plugins/claude-code/hooks/hooks.json), [recall hint](https://github.com/zilliztech/memsearch/blob/main/plugins/claude-code/hooks/user-prompt-submit.sh). |
| [Claude-Mem](https://github.com/thedotmack/claude-mem) | Its Claude Code hooks capture tool observations and failures, summarize at turn end, and inject context into new sessions. A worker processes captured material. | This can suit people who want work captured in the background. Check the installer, provider choice and host-specific behavior; installing a library alone does not activate its hooks. [Hook configuration](https://github.com/thedotmack/claude-mem/blob/main/plugin/hooks/hooks.json), [installation](https://github.com/thedotmack/claude-mem#quick-start). |
| [Memorix](https://github.com/AVIDS2/memorix) | Setup combines the host's available MCP, instructions, skills and hooks. Diagnostics inspect installed components and offer repairs. | Hook coverage and scope differ by host. Its automatic observations and reviewed long-term memory have separate admission rules. [Integration surfaces](https://github.com/AVIDS2/memorix/blob/main/docs/INTEGRATIONS.md), [event handling](https://github.com/AVIDS2/memorix/blob/main/src/hooks/handler.ts). |

These are source and documentation comparisons, checked on 2026-10-29. We have not
installed and compared these products in every host or measured their accuracy,
performance or cost. Follow their current setup instructions for your environment.

## Other useful options

| Option | Consider it when | What you maintain |
| --- | --- | --- |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | You want AI assistants and tools such as Obsidian to share readable Markdown, with MCP integration, a knowledge graph and semantic search. | A local Python installation and client configuration, plus your chosen local sync method; hosted service is an optional alternative. |
| [MCP Memory Service](https://github.com/doobidoo/mcp-memory-service) | You want an existing MCP/REST memory service, local vector search, a management dashboard and optional capture or synchronization features. | The service and its storage/model files. Sharing, remote access and optional model features require their own configuration. Core local memory operations do not require a cloud model API. |
| [Mem0](https://docs.mem0.ai/open-source/overview) | You are building an application and need programmatic memory operations, user scoping and configurable model/storage providers. | Application integration and the selected model, embedding and storage components. Its default quickstart uses model APIs; local providers can be configured. |

## Where Shared Agent Memory fits

This project keeps the shared store in files you control and lets your existing
Agent do the remembering. It may fit when you want local use, optional private
Git sharing, exact settings and corrections without operating a memory service.
Readable files and cross-Agent memory are also available in other projects.

Shared Agent Memory currently relies on the Agent and its available tools to complete
host integration. Its generated instructions ask the Agent to retrieve and save
proactively; it does not yet ship native lifecycle hooks. Writing an instruction
file does not prove it loads or gets followed in a new chat. Existing arbitrary
Markdown directories need a reviewed migration; the
installer automatically adopts only recognized knowledge repositories. Its core
retrieval uses scoped text search, configured aliases and lifecycle handling,
rather than general vector similarity. Review the
[compatibility record](compatibility.md) before relying on a specific host or
cross-device workflow.

A useful trial starts with ordinary work in a fictional project: “The preview
port is now 4311; update the deployment notes.” Do not ask it to remember anything.
Then open an independent session and ask it to continue deployment without giving
the port or reminding it about memory. Check whether it saved the change, retrieved
it and used the current value. Repeat in the other Agent or device you intend to
use. If any step fails, the integration still needs work.

Capabilities and installation requirements change. Consult the projects'
[Basic Memory local guide](https://docs.basicmemory.com/start-here/quickstart-local),
[MCP Memory Service documentation](https://github.com/doobidoo/mcp-memory-service/blob/main/README.md)
and [Mem0 quickstart](https://docs.mem0.ai/open-source/python-quickstart) for current
details. This comparison is based on their public documentation, not a measured
performance or cost benchmark.

The [reliability notes](reliability.md) describe specific lessons adopted from these
projects, the bugs reproduced here, and the regression checks added for them.
