# Reproducible demonstration and video outline

Use a disposable, owner-controlled private repository and fictional facts. Announce whether A/B are two directories on one machine, two independent Agents or two physical devices. Show the tool version, schema and actual permissions before claiming compatibility.

## Scene 1: connect

Give Agent A the public link and bootstrap prompt. Show capability observations and the resulting mode. With shell access, start from the install command in README. With GitHub tools only, follow `docs/github.md`. Display private-target identity and readback evidence. Missing scheduler/model capabilities remain visible.

## Scene 2: record and retrieve

On A, record this fictional user statement:

> For project Demo, status reports use concise paragraphs.

Use stable ID `demo-preference-v1`, scope `project:demo`, authority `user-stated` and source `user:demo-session:1`. Local reference:

```sh
python3 scripts/kb.py --root "/path/to/my-knowledge" capture --id demo-preference-v1 --kind preference --scope project:demo --statement "For project Demo, status reports use concise paragraphs." --source "user:demo-session:1" --authority user-stated --json
python3 scripts/kb.py --root "/path/to/my-knowledge" search "status reports" --scope project:demo --json
```

Read back the saved event. Sync through the configured private target. On B, retrieve it and cite the same ID and source. State separately when only local save or remote commit has been verified.

## Scene 3: correct

Record `demo-preference-v2`: “For project Demo, status reports use a short table.” Link the explicit correction:

```sh
python3 scripts/kb.py --root "/path/to/my-knowledge" capture --id demo-preference-v2 --kind preference --scope project:demo --statement "For project Demo, status reports use a short table." --source "user:demo-session:2" --authority user-stated --json
python3 scripts/kb.py --root "/path/to/my-knowledge" supersede --old-id demo-preference-v1 --new-id demo-preference-v2 --source "user:demo-session:2" --json
python3 scripts/kb.py --root "/path/to/my-knowledge" search "status reports" --scope project:demo --json
```

Show the new current record and retained old source. On B, refresh and verify correction propagation. A test in two local directories must be described as that test.

## Scene 4: boundaries

Ask an unrelated question with no record. Show scope/alias checks and freshness/coverage status before saying the information is unknown. Demonstrate offline local capture and later sync, or explicitly mark the unavailable condition. Append a remote event while the projection lags and verify ledger discovery. A screenshot of a created file alone does not prove discoverability.

## Scene 5: automation and exit

Show task plan, actual provider registration, trial result and last-success time as four distinct observations. Where the host is unavailable, show a planned task with the missing capability. Show data preserved after removing managed integration. Never stage a “successful background run” that was only a foreground simulation.

The final video should show source-backed continuity and honest limits. Token/cost claims require matched tasks, actual usage and a documented quality comparison; this script makes no savings claim.
