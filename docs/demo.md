# Try a complete memory workflow

Use a new demonstration directory and fictional information. The local walkthrough
needs Python and Git; remote sync and background tasks are optional. Run the
commands from the tool checkout and replace the sample paths.

## Save a preference

```sh
python3 scripts/ak.py --root "/path/to/demo-knowledge" init
python3 scripts/ak.py --root "/path/to/demo-knowledge" remember "Demo status reports use concise paragraphs." --source fictional:demo-session:1 --scope project:demo --id demo-preference
python3 scripts/ak.py --root "/path/to/demo-knowledge" recall "status reports" --scope project:demo
```

Check that the result contains the preference, its record ID and its source.
You can also ask your Agent: “For project Demo, remember that status reports use
concise paragraphs.”

## Correct it

```sh
python3 scripts/ak.py --root "/path/to/demo-knowledge" correct demo-preference "Demo status reports use a short table." --source fictional:demo-session:2
python3 scripts/ak.py --root "/path/to/demo-knowledge" recall "status reports" --scope project:demo
```

The current answer should use a short table. The old preference and its source
remain available in the record history. Copy the new ID returned by `correct`
if you want to correct that record again.

For structured output, place `--json` before the command:

```sh
python3 scripts/ak.py --root "/path/to/demo-knowledge" --json recall "status reports" --scope project:demo
```

## Continue in a new chat

Find the instruction file your Agent actually reads for the working project.
If that file is `AGENTS.md`, connect it like this:

```sh
python3 scripts/ak.py --root "/path/to/demo-knowledge" connect --instructions "/path/to/work-project/AGENTS.md"
python3 scripts/ak.py --root "/path/to/demo-knowledge" status
```

Open a new chat in that project's scope and ask: “How should I format a Demo
status report?” Do not include the answer in your question. Check that the Agent
retrieves the corrected preference and its source. A written instruction block
alone does not establish that the new chat loaded it. See [BOOTSTRAP](../BOOTSTRAP.md)
if your host uses a different instruction mechanism.

## Optional: share the same knowledge

To connect an existing private GitHub repository:

```sh
python3 scripts/ak.py --root "/path/to/demo-knowledge" connect-sync --repository owner/private-knowledge
python3 scripts/ak.py --root "/path/to/demo-knowledge" sync
```

Follow the [sync guide](sync.md) for repository verification and existing-history
requirements. On the other device, obtain the same knowledge repository, attach it
and connect that Agent's instructions. Retrieve the corrected preference and
compare its ID and source.

For a mobile drive, use the [removable storage flow](local-storage.md) on each
device. Keep local saving, remote synchronization and successful retrieval in
another chat distinct when describing what you observed.
