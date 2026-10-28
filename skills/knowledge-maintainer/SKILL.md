---
name: knowledge-maintainer
description: Capture authorized facts and corrections with provenance in the configured private knowledge base.
---

Resolve explicit tool and knowledge roots; read the user's POLICY.md and data version.
Use immutable schema-1 events. User statements retain that authority; inference stays
candidate. Capture exact values when authorized, keeping unnecessary values out of output.
Use --value-file for exact local values. Same-ID different content is a conflict, never
an overwrite. Corrections retain the original record and append a supersession relation.
Follow docs/github.md for no-CLI writes, expected-version/atomic operations and readback.
Verify saved bytes, discoverability and sync separately. Do not claim a hook or scheduler
ran from a written rule or plan. Only collect history within the user's chosen source scope.
Keep raw history, checkpoints and logs local; only share authorized normalized knowledge.
