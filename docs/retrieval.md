# Retrieval and freshness

Use `python3 scripts/retrieve.py --root "$KNOWLEDGE_ROOT" 'query' --scope project:atlas` for the foreground workflow. Tool and knowledge roots may differ; the disposable SQLite schema falls back to the tool distribution. The knowledge root must be an initialized private checkout. The public project itself is not a user's knowledge root.

The foreground entry point refreshes the disposable local index, expands configured aliases, and performs at most one pull of an already configured private remote when the snapshot is stale or there is no current evidence. Successful pull is followed by one local retry. It never creates a remote, supplies credentials, commits, pushes, or resolves conflicts. Existing identity, privacy, clean-worktree and rebase checks remain active. Offline and failed-sync results retain usable local evidence and explicitly bound the conclusion to the local snapshot. `--offline` prevents network calls. Freshness defaults to 3,600 seconds and is configurable under `retrieval.freshness_seconds`.

`kb.py search` remains a read-only lookup command. It does not claim the remote is current. To discover records added after the SQLite index was last refreshed, use the foreground entry point or run `kb.py update-index` first. A cold data volume that is unavailable is never silently replaced; Git-hot search continues and raw-history access is reported separately.

## Current evidence versus historical evidence

Every result includes its ID, scope, source or source location, authority, status, and available timestamps. `effective_status` applies expiry, future validity, explicit disputes, rejection and supersession. `current_eligible` is false for expired, disputed, archived, superseded, rejected, candidate and raw-history results. Historical records remain visible and are never deleted merely because they are no longer current. A result list with only historical or disputed evidence reports `current-value-unconfirmed`.

Ordering uses effective lifecycle, authority, query relevance, source time, then representation. Provenance time preference is `source_time`, `confirmed_at`, `observed_at`, then legacy record timestamps. An old statement imported today does not automatically become the newest source. Without source time metadata the client cannot infer it; importing clients must preserve source timestamps. Query relevance is measured on semantic text rather than ID length or metadata padding. Raw user/assistant conversation is evidence, not an automatically verified current fact.

Optional additive capture fields are `--source-time`, `--observed-at`, `--confirmed-at`, `--expires-at`, `--valid-from`, and `--fact-key`. Metadata is part of immutable event identity and is retained in resource projections. Reusing an ID with different metadata is a conflict. `MANIFEST.json` with a schema other than supported `1` blocks knowledge writes; reads and exports can still be used to diagnose or migrate deliberately. Older fixtures without a manifest remain readable for legacy compatibility.

Use a stable `fact_key` for assertions that describe the same property in the same scope. When one matching result has a `fact_key`, search also retrieves siblings with that identity (up to 20 identities per query and 1,000 candidates per sibling lookup). Two active IDs for the same key require an explicit supersession; both sources are returned as disputed even if the original query matched only one value. Similar words alone never imply the same fact. Known `conflicts_with` relationships and status `disputed` are also honored. Divergent supersession edges from the same old ID fail with a visible conflict instead of selecting the last timestamp; keep both events and resolve explicitly in a reviewed migration. The experimental client does not yet provide an automatic disputed-edge resolution command.

## Aliases and bounds

Configure exact scope aliases and query substitutions in the private configuration:

```json
{
  "retrieval": {
    "freshness_seconds": 3600,
    "scope_aliases": {"project:atlas": ["atlas", "星图"]},
    "query_aliases": {"release": ["发布计划", "上线计划"]}
  }
}
```

Scope aliases map to one exact scope; ambiguous alias definitions fail. Device-specific and project-specific records do not become global facts. Query substitution is bounded to eight queries. This is lexical retrieval with explicit aliases, not a semantic search model. Add aliases based on observed misses. Returned context remains bounded and truncation is marked; retrieve exact resources by stable ID when required.

Absence means “not found in the queried local/shared snapshot,” never “no Agent or device has this information.” Unshared raw history, unsaved proposals, truncated result sets and permission failures remain outside that conclusion. At large corpus sizes candidate limits can exclude evidence; the experimental scope is small personal knowledge repositories, and bounded conflict lookup is surfaced in `conflict_identity_coverage`.

## Evidence

Run:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_retrieval_public.py
PYTHONDONTWRITEBYTECODE=1 python3 evals/run_retrieval.py --output evals/current.json
```

The 24-question fixture is entirely fictional and uses the same material for the baseline and changed engine. Baseline was executed before the P2 changes: 18/24 correct current-first/abstention outcomes. Current is 24/24. These are deterministic local retrieval outcomes, not LLM answer accuracy, a product compatibility certification, a two-device test, or proof of token savings. Reports retain per-case IDs, expected/actual evidence, context characters and elapsed time. No model was invoked; end-to-end user clarification count and real background model cost are not measured. Foreground sync failure/retry tests inject the sync boundary and establish control flow only; real network compatibility evidence must be recorded separately.
