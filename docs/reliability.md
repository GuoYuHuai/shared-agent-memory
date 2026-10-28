# Reliability lessons from other memory projects

Reviewed on 2026-10-28 for the 0.2.1 experimental release. This is a record of
specific design lessons and reproducible checks, not a performance comparison.
Reported upstream issues are historical evidence, not claims that their current
releases still have those bugs.

## Find the right note, including when the index is unavailable

Basic Memory's [title weighting](https://github.com/basicmachines-co/basic-memory/blob/0884e204d84ac9d18124ad78601a80925643f901/src/basic_memory/repository/sqlite_search_query.py#L247-L256)
and [matched snippets](https://github.com/basicmachines-co/basic-memory/blob/0884e204d84ac9d18124ad78601a80925643f901/src/basic_memory/mcp/tools/search.py#L601-L613)
suggest useful checks for any text-based memory system. An exact title should
survive candidate selection; the displayed excerpt should show why a result matched.

Our regression fixtures include a long note with an exact title among 80 competing
body matches, a short Chinese query near the end of a note, and literal `%` and `_`
queries. Indexed retrieval now preserves those matches and returns relevant excerpts.

The closed Basic Memory [permission-error issue](https://github.com/basicmachines-co/basic-memory/issues/1007)
is another useful lesson: a failed scan must not be mistaken for an empty collection.
Shared Agent Memory now uses the same alias, scope and conflict handling when an index
refresh is busy or fails. It reports incomplete retrieval separately. A record
already saved to the ledger remains saved even if its index update fails; retrying
the same record ID repairs the projection without duplicating the fact.

Tests: `test_canonical_retrieval.py`, `test_retrieval_public.py`, `test_ak.py`.

## Keep meaning consistent across readers

Mem0's closed [scope-mutation issue](https://github.com/mem0ai/mem0/issues/6796)
and MCP Memory Service's [store-scoped fixes](https://github.com/doobidoo/mcp-memory-service/releases/tag/v12.0.0)
reinforce the need to test isolation through the whole read/write path.

We compared the Python and GitHub readers using the same fictional ledger. The
GitHub reader now respects candidate decisions, future-effective records, expiry
and explicit conflicts. Review events are not returned as current facts. Conflicting
decisions and invalid correction graphs cannot silently pick a winner. Facts in
another scope do not become conflicts in the requested project.

An accepted candidate also participates in conflict checks against other current
facts with the same identity. Sibling expansion checks the identity itself, so
an unrelated note that merely mentions its name is not added to the answer.

These checks cover ledger semantics. The GitHub reference adapter still does not
search canonical Markdown or implement local alias ranking; it cannot establish
that a fact is absent from the whole library.

Tests: `test_github.mjs`, `test_github_parity.py`.

## Check compatibility before changing a working copy

MCP Memory Service [v12.4.0](https://github.com/doobidoo/mcp-memory-service/releases/tag/v12.4.0)
checks peer protocol and schema compatibility before delta synchronization.
Shared Agent Memory uses Git, but needs the same precaution.

Before pulling or pushing, we inspect the committed knowledge manifest. An unknown
data schema or a different knowledge-library identity stops synchronization while
preserving both copies. The check runs again after a concurrent push changes the
remote. Different tool versions are allowed when the data schema remains compatible.

Tests: `test_git_sync.py`, using disposable Git repositories. Network identity and
privacy checks are substituted in those fixtures; they do not certify a live service.

## Keep setup small

These changes add no required model, database service, watcher or scheduled task.
Readable files and immutable records remain authoritative. The index is disposable;
private Git sync and history import remain optional. Existing checks for external
file edits already passed, so no background file watcher was added.

For broader differences and alternatives, see [Choosing a memory workflow](choosing.md).
