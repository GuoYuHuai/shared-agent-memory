# Capability evidence

Connection is selected by verified capability, not an Agent name allowlist. Brand and version are descriptive metadata for reproducing a result.

Every capability report contains `status`, `declared`, `verified_at`, `scope`, `evidence` and `blocker`. Status is one of `verified`, `declared`, `unknown`, `unavailable` or `permission_blocked`. Use an unavailable timestamp/evidence field explicitly when no observation exists. A tool name, an intended adapter and a model's confidence are not verification.

| Capability | Observation needed |
| --- | --- |
| `public_repo_read` | Read the selected release's entrypoint and files |
| `private_repo_read` | Read the intended owner's private repository |
| `private_repo_write` | Authorized identifiable write and readback; record version semantics |
| `private_repo_create` | Actual private repository creation and metadata readback |
| `atomic_commit` | A single commit containing all related changes |
| `local_files` | Read/write within the chosen directory |
| `local_exec` | Actual OS, interpreter, shell and Git execution |
| `persistent_instructions` | Owned integration readback and subsequent-session loading |
| `lifecycle_hooks` | Observed invocation for the claimed task/session event |
| `scheduler` | Create, query, disable and execution evidence |
| `unattended_model` | Actual background invocation, authorization and valid output |
| `history_access` | Specific source, format, range and permissions successfully read |

## Mode selection

| Capabilities | Available work | Must not claim |
| --- | --- | --- |
| Public read only | Read bootstrap and identify missing access | Personal knowledge connected |
| Private read | Retrieve evidence, prepare unsaved proposals | New facts persisted |
| Private read/write | Connector-based capture, readback and corrections | Local index/history installed |
| Local files/execution with Python and Git | Local retrieval/capture on the device or selected removable storage; remote sync and history collection are optional | A GitHub account, scheduler or model is required merely because local Git is used |
| Scheduler without model | Sync, deterministic capture and health checks | Semantic curation executed |
| Scheduler and background model | Bounded curation/review after actual registration and trial | Always-on execution while the host is asleep/offline |

The local reference currently requires Python and Git, but neither a GitHub account nor a remote. Pure local and removable storage are valid choices. Default installation only captures reusable knowledge from current work; full history backfill, raw archives and history-wide large indexes are opt-in. A scheduler is an optional capability, not a prerequisite for proactive capture or direct user requests.

Capabilities can combine independently: a cloud Agent may have private repository writes and scheduling but no access to a laptop's history. Session-only protocol use is valid when persistence is unavailable.

A path or mode label is not a physical-storage certification. Check actual access and mounting behavior; moving a knowledge root between two physical devices remains unverified until observed.

## Example of honest partial reporting

```json
{
  "agent": "Example Unknown Agent",
  "mode": "github",
  "capabilities": {
    "private_repo_read": {
      "status": "verified",
      "declared": true,
      "verified_at": "2030-01-01T12:00:00Z",
      "scope": "example-owner/example-private-repo",
      "evidence": "Fictional example: record read at a fixed commit",
      "blocker": null
    },
    "scheduler": {
      "status": "unknown",
      "declared": null,
      "verified_at": null,
      "scope": null,
      "evidence": null,
      "blocker": "No scheduler operation inspected"
    }
  }
}
```

This is a fictional report, not a compatibility result. For a real installation, record real versions, observed times and minimally sufficient evidence without publishing personal data.
