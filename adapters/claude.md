# Claude and other hosts

Start with [BOOTSTRAP](../BOOTSTRAP.md) and the tools available in the actual environment.
Connect the same knowledge directory rather than making a separate memory for each Agent.

When local execution and a project instruction file are supported, connect that file:

```sh
python3 scripts/ak.py --root "/path/to/knowledge" connect --instructions "/path/to/project/CLAUDE.md"
```

Use the current host's real instruction path. `CLAUDE.md` is an example, not a required
filename: the connector accepts any explicitly selected UTF-8 instruction file. It adds
one owned block, preserves other text and does not install global rules. The block routes
policy reading, foreground recall, capture, correction and authorized sync to the same
knowledge. A file with an unrelated or edited managed block is preserved for review.

Verify discovery in a fresh host session and retrieve a known fictional record. The command
only verifies file contents and ownership; it never claims the host loaded them. Without a
persistent instruction surface, use the current session and explain that scope. With GitHub
tools and no local execution, follow [the GitHub workflow](../docs/github.md).

Remove the owned connection before moving its paths or uninstalling:

```sh
python3 scripts/ak.py --root "/path/to/knowledge" disconnect --instructions "/path/to/project/CLAUDE.md"
```

Project text and knowledge remain. If the managed block was edited, follow the returned
recovery steps to preserve custom text outside the markers before disconnecting.

Doubao Desktop, WorkBuddy and future hosts use this same capability-based process. Missing
access or an unsupported instruction surface narrows what can be done; a product name does
not determine eligibility. Consult [compatibility](../docs/compatibility.md) for actual
validation evidence; these steps are not a certification of every host.
