"""P12: LoCoMo-style memory benchmark.

Measures:
- Recall@k: fraction of known facts retrieved in top-k
- Precision@k: fraction of retrieved results that are relevant
- MRR: Mean Reciprocal Rank of first correct result
- Edit Accuracy: fraction of validated edits that improve held-out score
"""
from __future__ import annotations

import os
import sqlite3
import random
from datetime import datetime, timezone


def evaluate_retrieval(db_path: str | None = None, k: int = 10) -> dict:
    """Evaluate retrieval across all entities with observations."""
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Get all entities with >=2 observations (need one to query, one to find)
        entities = conn.execute(
            "SELECT e.id, e.name FROM entities e "
            "JOIN observations o ON o.entity_id = e.id AND o.archived_at IS NULL "
            "GROUP BY e.id HAVING COUNT(o.id) >= 2"
        ).fetchall()
        if not entities:
            return {"error": "no entities with >=2 observations"}

        recall_sum = 0.0
        precision_sum = 0.0
        mrr_sum = 0.0
        n = 0

        for ent in entities:
            obs = conn.execute(
                "SELECT id, text FROM observations "
                "WHERE entity_id = ? AND archived_at IS NULL "
                "ORDER BY created_at DESC LIMIT 10",
                (ent["id"],),
            ).fetchall()
            if len(obs) < 2:
                continue
            n += 1
            # Hold out one observation as the target
            target = obs[0]
            known = obs[1:]
            known_ids = {o["id"] for o in known}

            # Query using a keyword from the target text
            query_words = target["text"].split()[:3]
            query = " ".join(query_words) if query_words else target["text"][:30]

            # Run FTS5 search
            try:
                fts_query = " OR ".join(query.split())
                results = conn.execute(
                    "SELECT rowid as observation_id FROM observations_fts "
                    "WHERE observations_fts MATCH ? ORDER BY rank LIMIT ?",
                    (fts_query, k),
                ).fetchall()
            except Exception:
                results = []

            result_ids = [r["observation_id"] for r in results]

            # Recall@k: did we find the target?
            if target["id"] in result_ids:
                recall_sum += 1.0
                rank = result_ids.index(target["id"]) + 1
                mrr_sum += 1.0 / rank

            # Precision@k: fraction of results that are known observations
            if result_ids:
                relevant = sum(1 for rid in result_ids if rid in known_ids)
                precision_sum += relevant / len(result_ids)

        return {
            "recall_at_k": round(recall_sum / n, 4) if n else 0,
            "precision_at_k": round(precision_sum / n, 4) if n else 0,
            "mrr": round(mrr_sum / n, 4) if n else 0,
            "n_queries": n,
            "k": k,
        }
    finally:
        conn.close()


def evaluate_edit_accuracy(db_path: str | None = None) -> dict:
    """Evaluate whether edit validation correlates with held-out improvement."""
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        epochs = conn.execute(
            "SELECT epoch_number, edits_total, edits_accepted, edits_rejected, "
            "heldout_score_before, heldout_score_after "
            "FROM epoch_boundaries ORDER BY epoch_number"
        ).fetchall()

        total_accepted = sum(e["edits_accepted"] or 0 for e in epochs)
        total_edits = sum(e["edits_total"] or 0 for e in epochs)

        improved = 0
        degraded = 0
        for e in epochs:
            if e["heldout_score_before"] is not None and e["heldout_score_after"] is not None:
                if e["heldout_score_after"] > e["heldout_score_before"]:
                    improved += 1
                else:
                    degraded += 1

        return {
            "total_edits": total_edits,
            "total_accepted": total_accepted,
            "acceptance_rate": round(total_accepted / total_edits, 4) if total_edits else 0,
            "epochs_improved": improved,
            "epochs_degraded": degraded,
            "n_epochs_with_scores": improved + degraded,
        }
    finally:
        conn.close()


def run_benchmark(db_path: str | None = None) -> dict:
    """Full benchmark suite."""
    return {
        "retrieval": evaluate_retrieval(db_path),
        "edit_accuracy": evaluate_edit_accuracy(db_path),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(run_benchmark(), indent=2))
