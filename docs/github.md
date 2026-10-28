# GitHub tools without a CLI

This is a first-class foreground path. Python, Node, a local checkout and an additional
model API are not prerequisites for an Agent following these steps with GitHub tools.
`adapters/github.mjs` is an executable connector contract for hosts able to import JS;
it has no shell, filesystem or authentication dependency. Other hosts follow the same
protocol directly with their tools.

1. Read repository metadata. Match the exact intended owner/name and require private
   visibility before writing personal knowledge. Reading the public toolkit is separate.
2. Read the user's POLICY.md and MANIFEST.json. Schema 1 is supported. An unknown writer
   schema pauses writes; do not overwrite existing policy or invent permissions.
3. Resolve a commit. List **all** pages of `ledger/` at that commit and read relevant
   files; `profile/`, `projects/`, `resources/`, `playbooks/` are additional canonical
   sources. The reference adapter's coverage is explicitly ledger-only. A whole-library
   absence claim requires all relevant canonical sources as well.
4. Do not use GitHub code-search indexing as the source of truth. Listing and reading
   ledger events includes writes whose projections/search indexes have not updated.
   Large collections need bounded, complete tree traversal. A truncated listing is an
   incomplete snapshot, never an empty answer. Use scoped/time partitions where supported.
5. With a pinned-ref tool, keep the same commit for the entire read. Otherwise compare
   branch head before/after reading, retry once on change and report incomplete on a
   second change. If a connector provides neither pins nor a generation/head comparison,
   snapshot consistency is unverified: return partial evidence, not a negative answer.
6. Create one uniquely identified immutable event at `ledger/by-id/ID.json` (a deterministic path independent of timestamp; older date partitions remain readable), schema
   matching `schema/record.schema.json`. Never replace a conflicting ID. Where possible
   commit a correction and its supersession edge atomically with an expected head lease.
   Otherwise append the new event first, then an edge with `kind: supersession`, `old_id`
   and `new_id`; report correction pending until the edge is read back. Both sources survive.
7. Read the saved file back at the returned commit; compare exact values. A second client
   must perform an independent snapshot to verify discovery. Report saved, read-back,
   discovery and cross-product verification separately. For readonly access return an
   **unsaved proposal** with intended path and event, without claiming capture success.

Connector bindings: `repository(name) -> {full_name,private}`;
`head(name,branch) -> commit`; `listFiles(name,ref,prefix) -> {paths,complete}`;
`read(name,ref,path) -> {content}`; optional `create(name,branch,path,content) -> {commit}`
with create-only conflict handling. `supportsPinnedRef` must describe actual behavior.
The binding must check permissions/API errors and complete pagination. Do not silently
substitute default-branch reads for unsupported ref reads. Interactive connector credentials
must not be assumed available to background CLI jobs.

The experimental reference supports ledger events and supersession conflicts. Local
candidate-resolution projections and advanced alias ranking are not yet fully mirrored
remotely; inspect resolution events and canonical records before adopting disputed values.
No claim of complete remote parity or large-library retrieval performance is made.

API basis: [GitHub repository contents](https://docs.github.com/en/rest/repos/contents)
documents ref selection, content SHA checks and directory-size limits. Hosts must use
their tool's real contract, not assume every connector exposes every REST capability.
