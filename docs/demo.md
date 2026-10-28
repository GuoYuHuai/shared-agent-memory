# Reproducible demonstration and video outline

Start with fictional facts in a disposable local knowledge directory. A GitHub account,
remote, scheduler or historical archive is not needed for the basic demonstration.
Optional scenes show shared GitHub knowledge, removable storage or background work
only when those paths are available and actually chosen.

Say whether A/B mean two directories on one machine, two independent Agents or two
physical devices. A removable mode flag or fixture directory does not establish a
physical-disk handoff. Show the version and evidence appropriate to the claim.

## Scene 1: give the Agent the link

Give Agent A the public repository URL and ask it to help connect the knowledge workflow.
It reads BOOTSTRAP.md, finds existing knowledge first and chooses a usable path.
For this basic demonstration it can initialize a new local directory without a remote:

```sh
python3 scripts/install.py --root "/path/to/demo-knowledge" --mode local
```

Use an actual new or recognized demo directory. See [BOOTSTRAP.md](../BOOTSTRAP.md)
and [local storage](local-storage.md) for installation and storage selection. Show that
history backfill and background model work have not been enabled by default.

## Scene 2: one sentence, saved and reusable

Tell A:

> For project Demo, status reports use concise paragraphs. Remember this preference.

Use stable ID `demo-preference-v1`, scope `project:demo`, authority `user-stated` and
source `user:demo-session:1`. Local reference:

```sh
python3 scripts/kb.py --root "/path/to/demo-knowledge" capture --id demo-preference-v1 --kind preference --scope project:demo --statement "For project Demo, status reports use concise paragraphs." --source "user:demo-session:1" --authority user-stated --json
python3 scripts/kb.py --root "/path/to/demo-knowledge" search "status reports" --scope project:demo --json
```

Read back the saved event and cite its ID and source. Later ask for a Demo status report
without restating the preference; show the Agent retrieving it before working.
In a normal task, the Agent should also capture a new reusable fact under the policy
without requiring the extra “remember” phrase. These use the same record path, not
separate memory systems. A written rule does not prove an automatic lifecycle hook.

## Scene 3: correct the preference

Tell A:

> Change that preference: Demo status reports should use a short table.

Create the corrected record and explicit relationship:

```sh
python3 scripts/kb.py --root "/path/to/demo-knowledge" capture --id demo-preference-v2 --kind preference --scope project:demo --statement "For project Demo, status reports use a short table." --source "user:demo-session:2" --authority user-stated --json
python3 scripts/kb.py --root "/path/to/demo-knowledge" supersede --old-id demo-preference-v1 --new-id demo-preference-v2 --source "user:demo-session:2" --json
python3 scripts/kb.py --root "/path/to/demo-knowledge" search "status reports" --scope project:demo --json
```

Show the new current record and the retained old source. Then ask an unrelated question
with no knowledge record. Show the scoped search and honest unknown result; do not invent
an answer or interpret incomplete coverage as absence. Basic local use is complete here.

## Optional scene: another Agent or device

For GitHub sharing, connect the user's own private repository and verify its identity,
permissions and visibility. After authorized sync, B retrieves the same record ID and
source. Correct on A and verify propagation to B. Report local save, remote commit,
discovery and cross-product/device observation separately. With connector tools only,
follow [docs/github.md](github.md), including snapshot checks and unprojected-event discovery.

For removable storage, use the actual mounted knowledge root and verify it on the next
device. Do not claim this physical scenario from a directory-only test. If it has not been
performed, label the scene as a procedure awaiting validation, not a successful demo.

## Optional scene: schedule, pause and exit

Only if scheduled maintenance is wanted, show its plan, actual provider registration,
trial and last-success evidence separately. Do not create a task just to make the basic
workflow look complete. Pause or disable the task, then demonstrate that direct retrieval
and capture still work. Show preserved knowledge after removing managed integration.

An unavailable scheduler does not invalidate the basic demonstration. No background model
or community review/merge schedule is required. History collection and larger archives
are advanced opt-in demonstrations with a user-chosen source range.

The video should show less repeated explanation, exact facts and traceable corrections.
It does not need a token-savings comparison. If a future demonstration makes a cost or
performance claim, that specific claim needs its own measured evidence.
