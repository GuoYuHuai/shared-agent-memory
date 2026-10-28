# Use your knowledge in a Codex project

Follow [BOOTSTRAP](../BOOTSTRAP.md) to connect an existing knowledge directory or create
one. Check the tools actually available in this session; GitHub-only access follows
[the shared GitHub workflow](../docs/github.md).

For local execution, connect the instruction file of the project where you work:

```sh
python3 scripts/ak.py --root "/path/to/knowledge" connect --instructions "/path/to/project/AGENTS.md"
```

Run this from the tool directory, using the real knowledge and project paths. The command
adds one owned block pointing to the existing knowledge. It preserves other instructions,
creates a missing instruction file within an existing project directory, and can be run
again without duplicating the block. It does not install global rules or create another
knowledge base. The same command accepts another host's actual instruction file; there is
no product-name check.

The block directs the Agent to read the knowledge policy, retrieve before repeating
questions, record reusable facts and corrections, and sync only when configured and
authorized. It contains routing paths rather than copies of private facts or values.

Start a fresh session in that project and verify that it finds the rule and retrieves a
known fictional record. File installation and new-session discovery are separate checks;
the connection command reports activation as unverified. If the host does not load project
instructions, use its supported rule surface or report current-session access.

To remove the connection while keeping the project instructions and knowledge:

```sh
python3 scripts/ak.py --root "/path/to/knowledge" disconnect --instructions "/path/to/project/AGENTS.md"
```

Disconnect before moving the knowledge/tool directory or uninstalling. If a managed block
has been edited, the command stops and names its receipt: move your custom text outside
the markers, restore the recorded block, then disconnect and reconnect. A connection from
another device or location is not proof of activation here.

Portable Skill sources remain in `skills/` for hosts that support them. Check existing
same-name content before installing a Skill. Scheduling and background models are optional
and require their own capability checks; neither is needed for basic recall and capture.

See the [compatibility record](../docs/compatibility.md) for tested environments and
[official customization guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
for host-specific discovery behavior.
