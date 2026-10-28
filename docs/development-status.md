# Current capabilities and ongoing work

Shared Agent Memory is experimental. The local command interface supports saving,
retrieving and correcting knowledge, connecting a host instruction file, and
optionally synchronizing through a private GitHub repository.

Start with [BOOTSTRAP](../BOOTSTRAP.md), the [local storage guide](local-storage.md)
or the [short walkthrough](demo.md). Agents with GitHub tools but no local command
execution can use the [connector workflow](github.md).

The core supports source references, exact values, scoped retrieval, retained
correction history and explicit freshness information. Historical imports and
background maintenance are optional and disabled in a new installation.

Areas for further work include:

- Verifying instruction discovery in new chats across more hosts and operating systems.
- Making migration from existing note collections easier while preserving their sources.
- Testing synchronization and removable storage between physical devices.
- Improving retrieval across different ways of expressing the same question.
- Extending optional history import and maintenance with clear progress and recovery.
- Validating physical removable-storage handoffs and network-filesystem behavior.
- Extracting focused modules from the large core files while preserving existing contracts.

The [portability review](portability-review.md) records current filesystem limits,
Git routing checks and the distinction between generic capture and history parsers.

See [compatibility](compatibility.md) for current environment coverage and
[acceptance](acceptance.md) for executed checks. Contributions should include a
small reproducible example and describe what was actually tested.
