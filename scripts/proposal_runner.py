#!/usr/bin/env python3
"""Accept a foreground Agent's proposal using the same host checks as Codex.

This is a file protocol: no model subscription, product name, or Shell inside
the proposing Agent is required. Only the applying host needs local execution.
"""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="validate and apply using isolated host transaction")
    args = parser.parse_args()
    os.environ["AI_KNOWLEDGE_ROOT"] = str(args.root.expanduser().resolve())
    import run_semantic_maintenance as host
    import semantic_batches as batches
    import workspace_lock
    batch = args.batch.resolve()
    try:
        manifest = batches.verify_batch_payload(batch)
        proposal = host.parse_proposal(args.proposal.resolve(), batch)
        if not args.apply:
            print(json.dumps({"ok": True, "state": "proposal-validated-not-applied", "batch_id": manifest["batch_id"], "events": len(proposal["events"])}))
            return 0
        with workspace_lock.acquire(host.ROOT):
            # Re-read under the shared mutation lock: pre-lock validation is
            # informational and cannot authorize applying a changed proposal.
            manifest = batches.verify_batch_payload(batch)
            proposal = host.parse_proposal(args.proposal.resolve(), batch)
            transaction = host.load_transaction(batch)
            if transaction is None:
                transaction = host.stage_proposal_transaction(batch, proposal)
            elif transaction.get("proposal_sha256") != __import__('hashlib').sha256(host.stable_json(proposal).encode()).hexdigest():
                raise ValueError("proposal differs from pending transaction")
            host.apply_prepared_transaction(batch, transaction)
            completed = batches.mark_complete(batch, model=None, reasoning_effort=None)
            host.release_transaction_ref(transaction)
        print(json.dumps({"ok": True, "state": "applied", "batch_id": manifest["batch_id"], "completion": completed}))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"ok": False, "state": "pending", "error": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
