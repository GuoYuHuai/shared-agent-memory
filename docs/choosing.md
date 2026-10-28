# Choosing a memory workflow

A short Markdown file is often enough. If you work in one project, have a few
stable preferences, and can ensure your Agent reads the file, start there. Add
sources and dated corrections when they help. A larger memory system should earn
its setup and maintenance cost by solving a problem you actually encounter.

Shared Agent Memory is an experimental option for people who repeatedly hand work
between Agents or devices and need exact configuration, provenance and explicit
corrections. Files and immutable events remain authoritative; indexes can be
rebuilt. Its local path uses Python and Git, with optional private Git sync. It
does not require a separate model service for foreground capture and retrieval.

These alternatives also deserve consideration:

| Option | Consider it when | What you maintain |
| --- | --- | --- |
| [Basic Memory](https://github.com/basicmachines-co/basic-memory) | You want AI assistants and tools such as Obsidian to share readable Markdown, with MCP integration, a knowledge graph and semantic search. | A local Python installation and client configuration, plus your chosen local sync method; hosted service is an optional alternative. |
| [MCP Memory Service](https://github.com/doobidoo/mcp-memory-service) | You want an existing MCP/REST memory service, local vector search, a management dashboard and optional capture or synchronization features. | The service and its storage/model files. Sharing, remote access and optional model features require their own configuration. Core local memory operations do not require a cloud model API. |
| [Mem0](https://docs.mem0.ai/open-source/overview) | You are building an application and need programmatic memory operations, user scoping and configurable model/storage providers. | Application integration and the selected model, embedding and storage components. Its default quickstart uses model APIs; local providers can be configured. |

Readable files, user-owned data and cross-Agent memory are not exclusive features
of Shared Agent Memory. Other projects offer stronger ready-made integrations and
semantic retrieval. Choose them when those capabilities are the main requirement.

Shared Agent Memory currently relies on the Agent and its available tools to complete
host integration. Writing an instruction file does not prove it loads in a new
chat. Existing arbitrary Markdown directories need a reviewed migration; the
installer automatically adopts only recognized knowledge repositories. Its core
retrieval uses scoped text search, configured aliases and lifecycle handling,
rather than general vector similarity. Review the
[compatibility record](compatibility.md) before relying on a specific host or
cross-device workflow.

A useful trial is small: record a fictional configuration, correct it, then ask
another new chat for the current value and its source. Confirm that it actually
retrieves the correction. That experience is more useful than a feature checklist
for deciding whether this workflow reduces repeated explanations in your work.

Capabilities and installation requirements change. Consult the projects'
[Basic Memory local guide](https://docs.basicmemory.com/start-here/quickstart-local),
[MCP Memory Service documentation](https://github.com/doobidoo/mcp-memory-service/blob/main/README.md)
and [Mem0 quickstart](https://docs.mem0.ai/open-source/python-quickstart) for current
details. This comparison is based on their public documentation, not a measured
performance or cost benchmark.

The [reliability notes](reliability.md) describe specific lessons adopted from these
projects, the bugs reproduced here, and the regression checks added for them.
