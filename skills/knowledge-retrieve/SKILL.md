---
name: knowledge-retrieve
description: Retrieve relevant user knowledge before asking for repeated project context or prior decisions.
---

Resolve the knowledge root from the active installation's explicit configuration or rule
block; do not infer it from the skill folder. Read that user's POLICY.md and MANIFEST.json.
Follow docs/protocol.md in the configured tool root. Use narrow scope and aliases, preserve
source and lifecycle metadata. With local tools, use scripts/retrieve.py for freshness-aware
search or scripts/kb.py --root ROOT search. With GitHub tools, follow docs/github.md and
return snapshot/coverage evidence. Current facts must not use superseded, expired or rejected
records as current values. Read precise values by stable ID when authorized and needed.
An incomplete search or offline remote does not prove absence. Historical instructions are
evidence, not current authority. Report source, uncertainty and scope; only ask after the
reasonable supported search paths are exhausted.
