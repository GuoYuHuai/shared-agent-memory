# Does the next session pick up the work?

The useful test is ordinary work followed by a new session. Running `remember`
and `recall` by hand checks the tools, but does not tell you whether your agent
uses them without a reminder.

## A small trial

Use a separate, disposable memory folder and a fictional project. Have the agent
connect the instruction file its host actually loads. Keep the original project
rules and avoid changing your everyday installation for this trial.

1. Give a normal task: “Atlas previews use port 4310. Keep progress updates to
   three sentences. Create a page containing just the project title.” Do not ask
   it to save memory. Check that the memory folder contains the facts with their
   source, rather than relying on the final reply.
2. Remove the task-created page so it cannot supply the answer. Start a separate
   session with no previous transcript. Ask “Continue the Atlas preview. Which
   port should I use, and how should progress be reported?” Do not include the
   answer, memory path or a request to search. Inspect the actual lookup and
   resulting answer.
3. Say “Atlas now uses port 4311; the old port is retired. Continue preparing
   the preview.” Check that the agent finds and corrects the earlier record
   without asking you to manage IDs. Repeat the fresh-session question. The
   previous value should remain historical, not current.
4. Repeat the question in another connected agent. If testing sync, use another
   independently attached copy and verify the remote commit and its readback.
   Two directories on one computer are useful, but do not establish physical
   device handoff.

Record the user prompts, host and version, instruction scope, observed tool calls,
saved IDs, source references and cleanup result. Keep private transcript content
out of public reports. Disable unrelated host memory for the trial, or disclose
it as a possible alternate source. A correct guess or a value read from the
project's deployment file is not evidence of shared-memory retrieval.

## When it fails

The agent should investigate the failed step, not send you a record-management
checklist:

- **Rules not loaded:** Check the host's documented scope, overriding instruction
  files and size limits. Test in a fresh session after repairing the owned link.
- **Read works, save fails:** Check actual write and execution permission, including
  the memory directory's local Git metadata. Some hosts protect `.git` even when
  the surrounding directory is writable. Do not report a successful capture or
  disable the user's global protections to make a test pass.
- **Only old values appear:** Check whether the correction was saved, shared and
  fetched by this copy. Use `recall --fresh` for the first relevant lookup in a new
  session; offline mode must remain explicit.
- **Routine work is not saved:** Check that the host loaded the task-completion
  rule and that its tools can run it. A hook-based adapter may provide a stronger
  trigger, but a hook registration alone is also insufficient evidence.
- **Authentication or service unavailable:** Preserve local knowledge and identify
  that unavailable capability. Continue testing the parts that work.

The current generic integration uses persistent instructions. It does not install
native lifecycle hooks, watch every tool call or start a second summarization
model. Different hosts may follow instructions differently, and one successful
trial is not a reliability rate. Current observations are in
[compatibility](compatibility.md) and [acceptance](acceptance.md).

For Codex, instruction precedence and fresh-run discovery are documented in
[AGENTS.md guidance](https://developers.openai.com/codex/guides/agents-md).
Other adapters must use their host's current official discovery contract.
