# AI Knowledge development

Read BOOTSTRAP.md for user onboarding; docs/protocol.md defines the shared contract.
This repository contains tools and fictional fixtures, not the user's knowledge.
Use a separate, explicit knowledge root. Preserve immutable records, exact authorized
values, provenance and user policy. Never connect tests to a user's working knowledge root.
Run Python unittest discovery and Node tests/test_github.mjs; report platform skips.
Use temporary directories with cleanup, avoid persistent model/scheduler side effects.
Capabilities require evidence, not product-name assumptions. Do not promote planned,
registered, mocked or documented behavior to real execution success.
Release exports use TOOL_MANIFEST.json and scripts/release_check.py; public history must
start independently of any private repository. Publish only reviewed tool sources,
documentation and fictional fixtures; never include a user's knowledge or private history.
