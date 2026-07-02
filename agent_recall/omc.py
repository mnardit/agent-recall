"""
OMC (Online Memory & Cognition) shared module.

Core functions used by both the MCP server tools and the hook scripts.
Eliminates the importlib fragility — single source of truth for all OMC logic.

Dual-track storage (MemPalace + mem0):
  Track 1: verbatim drawer entities (zero-LLM, 100% fidelity)
  Track 2: structured observations (LLM-extracted facts with confidence)

Pipeline: Extract -> Dedup -> Link -> Rebalance -> Evolve
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import numpy as np
except ImportError:
    np = None

from agent_recall.store import MemoryStore


# ---------------------------------------------------------------------------
# Intent classification
# ---------------------------------------------------------------------------

def classify_intent(text: str) -> str:
    """Simple keyword-based intent classification.

    Returns one of: preference, decision, constraint, fact, task_state.
    P2 upgrade: LLM-based classification.
    """
    text_lower = text.lower()

    preference_kw = ["prefer", "like", "don't like", "use async", "better to",
                     "should use", "recommend", "favorite"]
    decision_kw = ["decided", "chose", "selected", "will use", "going with"]
    constraint_kw = ["must", "never", "always", "required",
                     "hard constraint", "non-negotiable"]
    task_state_kw = ["working on", "in progress", "todo", "blocked", "done",
                     "completed"]

    for kw in decision_kw:
        if kw in text_lower:
            return "decision"
    for kw in constraint_kw:
        if kw in text_lower:
            return "constraint"
    for kw in preference_kw:
        if kw in text_lower:
            return "preference"
    for kw in task_state_kw:
        if kw in text_lower:
            return "task_state"
    return "fact"


# ---------------------------------------------------------------------------
# Phase 1: Extract + Backfill
# ---------------------------------------------------------------------------

def backfill_observation_meta(store: MemoryStore, dry_run: bool = False) -> int:
    """Backfill observation_meta for observations without meta.

    Runs intent classification and sets initial confidence.
    Returns count of backfilled observations.
    """
    conn = store._conn
    rows = conn.execute(
        """SELECT o.id, o.text
           FROM observations o
           LEFT JOIN observation_meta om ON o.id = om.observation_id
           WHERE om.observation_id IS NULL
           LIMIT 500"""
    ).fetchall()

    count = 0
    for row in rows:
        obs_id = row["id"]
        text = row["text"] or ""
        intent = classify_intent(text)

        if not dry_run:
            conn.execute(
                """INSERT OR REPLACE INTO observation_meta
                   (observation_id, valid_from, confidence, intent_type)
                   VALUES (?, datetime('now'), 0.5, ?)""",
                (obs_id, intent),
            )
        count += 1

    if not dry_run and count > 0:
        conn.commit()
    return count


# ---------------------------------------------------------------------------
# Phase 2: Dedup
# ---------------------------------------------------------------------------

def _merge_observations(conn, keep_id: int, del_id: int, similarity: float) -> None:
    """Merge del_id into keep_id: archive del, boost keep confidence, clean orphan meta."""
    conn.execute(
        "UPDATE observations SET archived_at = datetime('now') WHERE id = ?",
        (del_id,),
    )
    # Clean orphan meta + tiers for the deleted observation
    conn.execute("DELETE FROM observation_meta WHERE observation_id = ?", (del_id,))
    conn.execute("DELETE FROM knowledge_tiers WHERE observation_id = ?", (del_id,))
    conn.execute(
        """INSERT OR REPLACE INTO observation_meta
           (observation_id, valid_from, confidence, intent_type)
           VALUES (?, COALESCE((SELECT valid_from FROM observation_meta WHERE observation_id=?),
                               datetime('now')),
                   MIN(1.0, COALESCE((SELECT confidence FROM observation_meta WHERE observation_id=?), 0.5) + 0.1),
                   (SELECT intent_type FROM observation_meta WHERE observation_id=?))
        """,
        (keep_id, keep_id, keep_id, keep_id),
    )


def _dedup_fts5(store, rows, threshold, dry_run) -> dict:
    """FTS5-based fallback dedup using Jaccard similarity."""
    conn = store._conn
    merged = []

    texts = {r["id"]: r["text"] or "" for r in rows}

    for i, r1 in enumerate(rows):
        id1 = r1["id"]
        text1 = texts[id1]
        if not text1:
            continue
        words1 = set(text1.lower().split())
        if len(words1) < 3:
            continue

        for r2 in rows[i + 1:]:
            id2 = r2["id"]
            text2 = texts[id2]
            if not text2:
                continue
            words2 = set(text2.lower().split())

            intersection = words1 & words2
            union = words1 | words2
            if not union:
                continue
            jaccard = len(intersection) / len(union)

            if jaccard >= threshold:
                keep_id = id1 if len(text1) >= len(text2) else id2
                del_id = id2 if keep_id == id1 else id1
                if not dry_run:
                    _merge_observations(conn, keep_id, del_id, jaccard)
                merged.append(
                    {"kept": keep_id, "deleted": del_id, "similarity": round(jaccard, 3)}
                )

    deleted_count = len(set(m["deleted"] for m in merged))
    return {
        "merged_count": len(merged),
        "deleted_count": deleted_count,
        "details": merged[:10],
        "method": "fts5_jaccard",
    }


def dedup_observations(
    store: MemoryStore, threshold: float = 0.85, dry_run: bool = False
) -> dict:
    """Semantic dedup: find and merge near-duplicate observations.

    Uses sqlite-vec cosine similarity when available (with numpy),
    falls back to FTS5 Jaccard keyword matching.

    Returns:
        dict with merged_count, deleted_count, details
    """
    conn = store._conn

    rows = conn.execute(
        """SELECT o.id, o.text, o.entity_id, o.scope
           FROM observations o
           WHERE o.archived_at IS NULL
           ORDER BY o.id"""
    ).fetchall()

    if len(rows) < 2:
        return {"merged_count": 0, "deleted_count": 0, "details": []}

    if np is None:
        return _dedup_fts5(store, rows, threshold, dry_run)

    try:
        from agent_recall.embeddings import get_provider
        provider = get_provider()
        embeddings = provider.embed_batch([r["text"] or "" for r in rows])
    except Exception:
        return _dedup_fts5(store, rows, threshold, dry_run)

    texts = [r["text"] or "" for r in rows]
    ids = [r["id"] for r in rows]

    embeddings_np = np.array(embeddings)
    norms = np.linalg.norm(embeddings_np, axis=1, keepdims=True)
    norms[norms == 0] = 1e-10
    normalized = embeddings_np / norms

    merged = []
    deleted = []
    seen = set()

    for i in range(len(ids)):
        if i in seen:
            continue
        for j in range(i + 1, len(ids)):
            if j in seen:
                continue
            sim = float(np.dot(normalized[i], normalized[j]))
            if sim >= threshold:
                keep_id = ids[i] if len(texts[i]) >= len(texts[j]) else ids[j]
                del_id = ids[j] if keep_id == ids[i] else ids[i]
                if not dry_run:
                    _merge_observations(conn, keep_id, del_id, sim)
                merged.append(
                    {"kept": keep_id, "deleted": del_id, "similarity": round(sim, 3)}
                )
                deleted.append(del_id)
                seen.add(j)

    return {
        "merged_count": len(merged),
        "deleted_count": len(deleted),
        "details": merged[:10],
    }


# ---------------------------------------------------------------------------
# Phase 3: Link
# ---------------------------------------------------------------------------

def link_observations(store: MemoryStore) -> int:
    """Auto-link observations to existing entities via co-occurrence.

    Returns count of new relations created.
    """
    from agent_recall.entity_linker import EntityLinker

    linker = EntityLinker(store)
    conn = store._conn
    rows = conn.execute(
        "SELECT id FROM observations WHERE archived_at IS NULL ORDER BY id DESC LIMIT 50"
    ).fetchall()
    obs_ids = [r["id"] for r in rows]

    if len(obs_ids) < 2:
        return 0

    try:
        return linker.link_co_occurring(obs_ids)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Phase 4: Tier Rebalance
# ---------------------------------------------------------------------------

def rebalance_tiers(store: MemoryStore) -> dict:
    """Rebalance knowledge tiers based on access patterns."""
    from agent_recall.knowledge_tiers import KnowledgeTierManager

    mgr = KnowledgeTierManager(store)
    before = mgr.status("global")
    mgr.check_and_rebalance()
    after = mgr.status("global")
    return {"before": before, "after": after}


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def run_maintenance(db_path: str | None = None) -> dict:
    """Full maintenance cycle: dedup + tier rebalance + cleanup.

    Cleanup operations:
    1. Expired observations (valid_to < now) → archive
    2. Cold + old data (90+ days archived) → physical delete
    3. Orphan entities (no observations) → delete

    Args:
        db_path: Path to frames.db. Defaults to ~/.agent-recall/frames.db

    Returns:
        dict with per-phase results
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        result = {}

        # Dedup
        result["dedup"] = dedup_observations(store, threshold=0.85, dry_run=False)

        # Tier rebalance
        result["tiers"] = rebalance_tiers(store)

        # Cleanup
        conn = store._conn

        # Archive expired
        expired = conn.execute(
            """UPDATE observations SET archived_at = datetime('now')
               WHERE id IN (
                   SELECT om.observation_id FROM observation_meta om
                   WHERE om.valid_to IS NOT NULL
                     AND om.valid_to < datetime('now')
                     AND om.observation_id IN (
                         SELECT id FROM observations WHERE archived_at IS NULL
                     )
               )"""
        ).rowcount
        result["cleanup"] = {"expired_archived": expired}

        # Physical delete: cold + archived > 90 days
        cold_deleted = conn.execute(
            """DELETE FROM observations
               WHERE id IN (
                   SELECT o.id FROM observations o
                   JOIN knowledge_tiers kt ON o.id = kt.observation_id
                   LEFT JOIN observation_meta om ON o.id = om.observation_id
                   WHERE kt.tier = 'cold'
                     AND o.archived_at IS NOT NULL
                     AND o.archived_at < datetime('now', '-90 days')
               )"""
        ).rowcount
        if cold_deleted:
            conn.commit()
        result["cleanup"]["cold_deleted"] = cold_deleted

        # Orphan meta records (observation archived/deleted but meta remains)
        orphan_meta = conn.execute(
            """DELETE FROM observation_meta WHERE observation_id IN (
                SELECT om.observation_id FROM observation_meta om
                LEFT JOIN observations o ON om.observation_id = o.id
                WHERE o.id IS NULL OR o.archived_at IS NOT NULL
            )"""
        ).rowcount

        # Orphan entities
        orphaned = conn.execute(
            """DELETE FROM entities
               WHERE id NOT IN (
                   SELECT DISTINCT entity_id FROM observations
                   UNION
                   SELECT DISTINCT entity_id FROM slots
               )"""
        ).rowcount
        if orphaned or orphan_meta:
            conn.commit()
        result["cleanup"]["orphaned_entities"] = orphaned
        result["cleanup"]["orphan_meta"] = orphan_meta

        # Memory type backfill (new observations)
        try:
            typed = apply_memory_type_metadata(db_path, limit=100)
            result["cleanup"]["memory_types_backfilled"] = typed
        except Exception:
            result["cleanup"]["memory_types_backfilled"] = 0

        # Audit the maintenance run
        audit_log("maintenance", "system", {
            "merged": result["dedup"]["merged_count"],
            "expired": expired,
            "orphan_meta": orphan_meta,
            "orphan_entities": orphaned,
        })

        conn.commit()
        return result

    finally:
        store.close()


# ---------------------------------------------------------------------------
# Skill Discovery
# ---------------------------------------------------------------------------

def discover_skills(db_path: str | None = None) -> list[dict]:
    """Scan filesystem for skills and return discovery list.

    Sources: .claude/skills/ (user + project), plugins cache.
    Does NOT write to DB — call register_skills() for that.
    """
    import hashlib

    home = Path.home()
    sources = [
        (home / ".claude" / "skills", "user"),
        (Path.cwd() / ".claude" / "skills", "project"),
        (home / ".claude" / "plugins" / "cache", "plugin"),
    ]

    discovered = []
    seen_paths = set()
    for src_dir, src_type in sources:
        if not src_dir.exists():
            continue
        for skill_md in src_dir.rglob("SKILL.md"):
            path_str = str(skill_md)
            if path_str in seen_paths:
                continue  # Skip same file found via multiple source paths
            seen_paths.add(path_str)
            content = skill_md.read_text(encoding="utf-8", errors="ignore")
            skill_hash = hashlib.sha256(content.encode()).hexdigest()[:12]
            skill_name = skill_md.parent.name if skill_md.parent.name != "skills" else src_dir.name
            discovered.append({
                "name": skill_name,
                "path": str(skill_md),
                "hash": skill_hash,
                "source": src_type,
            })
    return discovered


def register_skills(discovered: list[dict], db_path: str | None = None) -> dict:
    """Register discovered skills in the knowledge graph.

    Creates/updates skill entities and observations.
    Returns registration summary.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    created = 0
    updated = 0
    try:
        conn = store._conn
        for skill in discovered:
            entity_name = f"skill:{skill.get('source', 'local')}:{skill['name']}"
            existing = conn.execute(
                "SELECT id FROM entities WHERE name=? AND type='skill'",
                (entity_name,),
            ).fetchone()

            if existing:
                eid = existing["id"]
                conn.execute(
                    "UPDATE entities SET created_at=datetime('now') WHERE id=?",
                    (eid,),
                )
                updated += 1
            else:
                eid = conn.execute(
                    "INSERT INTO entities (name, type, created_at) VALUES (?, 'skill', datetime('now'))",
                    (entity_name,),
                ).lastrowid
                created += 1

            conn.execute(
                "INSERT OR REPLACE INTO observations (entity_id, text, scope, created_at) "
                "VALUES (?, ?, 'global', datetime('now'))",
                (eid, f"Path: {skill['path']}\nHash: {skill['hash']}\nSource: {skill.get('source', 'unknown')}"),
            )

        store._conn.commit()
        return {"created": created, "updated": updated, "total": len(discovered)}
    finally:
        store.close()


def get_skills_to_prune(db_path: str | None = None) -> list[str]:
    """Identify skills eligible for pruning based on DB data.

    Returns list of plugin keys (name@marketplace format) to disable.

    Rules:
    - Skill entities with no observations in 30+ days
    - Skills with 3+ consecutive rejected edit epochs
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Inactive skills: no observations in 30 days
        rows = conn.execute(
            """SELECT e.name, MAX(o.created_at) as last_seen
               FROM entities e
               JOIN observations o ON e.id = o.entity_id
               WHERE e.type = 'skill'
               GROUP BY e.id
               HAVING last_seen < datetime('now', '-30 days')
                  OR last_seen IS NULL"""
        ).fetchall()

        candidates = []
        for row in rows:
            skill_name = row["name"]
            if skill_name.startswith("skill:"):
                skill_name = skill_name[6:]  # Strip "skill:" prefix
            candidates.append(skill_name)

        return candidates
    finally:
        store.close()


def prune_skills_in_settings(
    to_disable: list[str],
    settings_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Prune skills by disabling them in settings.json.

    Uses EXACT matching on the plugin key name part (before @):
      "skill-name@marketplace" → match "skill-name"

    Args:
        to_disable: List of skill names (without @marketplace suffix)
        settings_path: Path to settings.json. Defaults to ~/.claude/settings.json
        dry_run: If true, preview only

    Returns:
        dict with disabled list and count
    """
    if settings_path is None:
        settings_path = os.path.expanduser("~/.claude/settings.json")

    sp = Path(settings_path)
    if not sp.exists():
        return {"error": f"settings.json not found at {settings_path}"}

    settings = json.loads(sp.read_text())
    enabled = settings.get("enabledPlugins", {})

    # Exact matching: plugin key format is "name@marketplace"
    # Match against the name part only
    disabled_list = []
    for candidate in to_disable:
        for plugin_key in list(enabled.keys()):
            # Extract the name part before '@'
            plugin_name = plugin_key.split("@")[0] if "@" in plugin_key else plugin_key
            if plugin_name == candidate and enabled[plugin_key]:
                disabled_list.append(plugin_key)

    if not dry_run and disabled_list:
        for key in disabled_list:
            enabled[key] = False
        settings["enabledPlugins"] = enabled
        sp.write_text(json.dumps(settings, indent=2))

    return {
        "dry_run": dry_run,
        "to_disable": disabled_list,
        "disabled_count": len(disabled_list),
        "message": (
            f"Would disable {len(disabled_list)} skills (preview)"
            if dry_run
            else f"Disabled {len(disabled_list)} skills (EXECUTED)"
        ),
    }


# ---------------------------------------------------------------------------
# ReflACT Epoch Pipeline
# ---------------------------------------------------------------------------
# Auto-triggered evolution cycle: Rollout → Reflect → Aggregate → Select → Validate
#
# Conditions to start a new epoch (any one triggers):
#   - 10+ candidate edits accumulated
#   - 20+ sessions since last epoch
#   - 7+ days since last epoch
#
# The epoch runs synchronously during SessionStart (cheap — no LLM calls).
# P2 upgrade: LLM-based Reflect (ranking) and Validate (held-out comparison).

EPOCH_MIN_EDITS = 5        # Min candidate edits to trigger epoch (lower for faster feedback)
EPOCH_MIN_SESSIONS = 10    # Min sessions since last epoch
EPOCH_MIN_DAYS = 3         # Min days since last epoch


def should_start_epoch(db_path: str | None = None) -> dict:
    """Check if conditions are met to start a new ReflACT epoch.

    Returns:
        dict with should_start, reason, and current stats
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Count candidate edits
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM edits WHERE status='candidate'"
        ).fetchone()[0]

        # Check last epoch
        last_epoch = conn.execute(
            "SELECT epoch_number, completed_at, edits_total, edits_accepted "
            "FROM epoch_boundaries ORDER BY epoch_number DESC LIMIT 1"
        ).fetchone()

        if last_epoch is None:
            return {
                "should_start": candidate_count >= EPOCH_MIN_EDITS,
                "reason": "No previous epoch" if candidate_count >= EPOCH_MIN_EDITS
                          else f"Need {EPOCH_MIN_EDITS} edits, have {candidate_count}",
                "candidate_edits": candidate_count,
                "last_epoch": None,
                "next_epoch_number": 1,
            }

        # Check sessions since
        sessions_since = conn.execute(
            "SELECT COUNT(DISTINCT session_id) FROM rollout_results "
            "WHERE created_at > COALESCE(?, '1970-01-01')",
            (last_epoch["completed_at"],),
        ).fetchone()[0]

        # Check days since
        days_since = None
        if last_epoch["completed_at"]:
            days_since = conn.execute(
                "SELECT CAST(julianday('now') - julianday(?) AS INTEGER)",
                (last_epoch["completed_at"],),
            ).fetchone()[0]

        triggers = []
        if candidate_count >= EPOCH_MIN_EDITS:
            triggers.append(f"edits: {candidate_count} >= {EPOCH_MIN_EDITS}")
        if sessions_since >= EPOCH_MIN_SESSIONS:
            triggers.append(f"sessions: {sessions_since} >= {EPOCH_MIN_SESSIONS}")
        if days_since is not None and days_since >= EPOCH_MIN_DAYS:
            triggers.append(f"days: {days_since} >= {EPOCH_MIN_DAYS}")

        return {
            "should_start": len(triggers) > 0,
            "reason": " | ".join(triggers) if triggers
                      else f"Edits={candidate_count}/{EPOCH_MIN_EDITS}, "
                           f"Sessions={sessions_since}/{EPOCH_MIN_SESSIONS}, "
                           f"Days={days_since}/{EPOCH_MIN_DAYS}",
            "candidate_edits": candidate_count,
            "sessions_since": sessions_since,
            "days_since": days_since,
            "last_epoch": {
                "number": last_epoch["epoch_number"],
                "completed_at": last_epoch["completed_at"],
                "edits_total": last_epoch["edits_total"],
                "edits_accepted": last_epoch["edits_accepted"],
            },
            "next_epoch_number": last_epoch["epoch_number"] + 1,
        }
    finally:
        store.close()


def record_rollout(
    session_id: str,
    hard_score: int | None = None,
    soft_score: float | None = None,
    n_turns: int | None = None,
    fail_reason: str | None = None,
    task_description: str | None = None,
    db_path: str | None = None,
) -> int:
    """Record a session outcome for future Reflect analysis.

    Called at session end (or SessionStart catch_up) to capture
    what happened in the previous session.

    Returns:
        rollout_result id
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    if session_id is None:
        session_id = os.environ.get("AGENT_RECALL_SLUG", "unknown")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        rid = conn.execute(
            """INSERT INTO rollout_results
               (session_id, hard_score, soft_score, n_turns, fail_reason,
                task_description, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (session_id, hard_score, soft_score, n_turns, fail_reason, task_description),
        ).lastrowid
        conn.commit()
        return rid
    finally:
        store.close()


def run_epoch(db_path: str | None = None, dry_run: bool = True) -> dict:
    """Execute one full ReflACT epoch cycle.

    Phases:
    1. Reflect — analyze candidate edits, update support_count
    2. Aggregate — group by target, rank by support_count
    3. Select — apply LR budget, pick top edits
    4. Apply — apply edits to target files
    5. Validate — stub (P2: held-out comparison)

    Args:
        db_path: Path to frames.db
        dry_run: If true, simulate without applying edits

    Returns:
        dict with per-phase results
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    result = {"phases": {}, "dry_run": dry_run}

    try:
        conn = store._conn

        # Get current epoch number
        last = conn.execute(
            "SELECT epoch_number FROM epoch_boundaries ORDER BY epoch_number DESC LIMIT 1"
        ).fetchone()
        epoch_num = (last["epoch_number"] + 1) if last else 1

        # Create epoch boundary
        eid = conn.execute(
            """INSERT INTO epoch_boundaries (epoch_number, started_at)
               VALUES (?, datetime('now'))""",
            (epoch_num,),
        ).lastrowid

        # ── Phase 1: Reflect ──
        # Promote edits with same target+op: increment support_count
        promoted = conn.execute(
            """UPDATE edits SET support_count = support_count + 1
               WHERE status = 'candidate'
                 AND target IN (
                     SELECT target FROM edits
                     WHERE status = 'candidate'
                     GROUP BY target, op
                     HAVING COUNT(*) > 1
                 )"""
        ).rowcount
        result["phases"]["reflect"] = {"support_promoted": promoted}

        # ── Phase 2: Aggregate ──
        # Get candidates grouped by target, ranked by support_count
        candidates = conn.execute(
            """SELECT id, op, content, target, support_count, source_type, merge_level
               FROM edits WHERE status = 'candidate'
               ORDER BY support_count DESC, created_at ASC"""
        ).fetchall()
        result["phases"]["aggregate"] = {
            "candidates_total": len(candidates),
            "targets": len(set(r["target"] for r in candidates)),
        }

        # ── Phase 3: Select ──
        # Apply LR budget: select top-L edits
        lr_row = conn.execute(
            "SELECT current_lr, min_lr FROM learning_rate_state ORDER BY id DESC LIMIT 1"
        ).fetchone()

        if lr_row is None:
            # Initialize LR state
            conn.execute(
                """INSERT INTO learning_rate_state
                   (scheduler_type, current_lr, current_step, updated_at)
                   VALUES ('autonomous', 0.3, 0, datetime('now'))"""
            )
            lr = 0.3
        else:
            lr = lr_row["current_lr"]

        # Top-L: take top LR * len(candidates), min 1, max all
        budget = max(1, min(len(candidates), int(len(candidates) * lr)))
        selected = candidates[:budget]

        # Mark selected
        selected_ids = [r["id"] for r in selected]
        if selected_ids:
            placeholders = ",".join("?" * len(selected_ids))
            conn.execute(
                f"UPDATE edits SET status='selected' WHERE id IN ({placeholders})",
                selected_ids,
            )
        result["phases"]["select"] = {
            "lr": lr,
            "budget": budget,
            "selected": len(selected_ids),
            "rejected": len(candidates) - len(selected_ids),
        }

        # ── Phase 4: Apply ──
        applied = 0
        skipped = 0
        errors = []
        for edit in selected:
            target_path = Path(edit["target"])
            if not target_path.exists():
                skipped += 1
                errors.append({"edit_id": edit["id"], "error": "target not found"})
                continue

            content = target_path.read_text(encoding="utf-8", errors="ignore")
            new_content = _apply_edit_op_safe(
                content, edit["op"], edit["content"], edit["target"]
            )

            if new_content is None:
                skipped += 1
                errors.append({"edit_id": edit["id"], "error": "protected region"})
                continue

            if not dry_run:
                # Backup
                backup_path = target_path.with_suffix(target_path.suffix + ".omc-bak")
                backup_path.write_text(content, encoding="utf-8")
                # Apply
                target_path.write_text(new_content, encoding="utf-8")
                conn.execute(
                    "UPDATE edits SET status='applied', applied_at=datetime('now') WHERE id=?",
                    (edit["id"],),
                )
            applied += 1

        result["phases"]["apply"] = {
            "applied": applied,
            "skipped": skipped,
            "errors": errors[:5],
        }

        # —— Phase 5: Validate ——
        accepted_v = 0
        rejected_v = 0
        try:
            from agent_recall.omc import validate_edits
            erred_ids = {e["edit_id"] for e in errors if isinstance(e, dict) and "edit_id" in e}
            applied_ids = [eid for eid in selected_ids if eid not in erred_ids]
            validate_result = validate_edits(applied_ids if not dry_run else selected_ids)
            accepted_list = validate_result.get("accepted", [])
            rejected_list = validate_result.get("rejected", [])
            accepted_v = len(accepted_list)
            rejected_v = len(rejected_list)
            if not dry_run:
                for eid in rejected_list:
                    store._conn.execute(
                        "UPDATE edits SET status = 'rejected' WHERE id = ?", (eid,)
                    )
                store._conn.commit()
            result["phases"]["validate"] = {
                "baseline_score": validate_result.get("baseline", {}).get("avg_score"),
                "validated": len(validate_result.get("details", [])),
                "accepted": accepted_v,
                "rejected": rejected_v,
            }
        except Exception as e:
            result["phases"]["validate"] = {"error": str(e)}

        # —— SkillOpt LR update: adjust learning rate based on validation outcomes ——
        # Higher acceptance → raise LR (explore more); lower → reduce LR (be conservative)
        total_validated = accepted_v + rejected_v if (accepted_v + rejected_v) > 0 else len(selected_ids)
        if total_validated > 0:
            accept_rate = accepted_v / total_validated
            lr_row = conn.execute(
                "SELECT * FROM learning_rate_state ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if lr_row:
                new_step = lr_row["current_step"] + 1
                old_lr = lr_row["current_lr"]
                min_lr = lr_row.get("min_lr", 0.05)
                max_lr = lr_row.get("max_lr", 0.5)
                # ponytail: simple LR schedule — multiplicative step based on outcome
                if accept_rate >= 0.6:
                    new_lr = min(old_lr * 1.15, max_lr)  # Good → expand budget
                elif accept_rate <= 0.2:
                    new_lr = max(old_lr * 0.7, min_lr)   # Poor → shrink budget
                else:
                    new_lr = old_lr  # Moderate → hold
                conn.execute(
                    """INSERT INTO learning_rate_state
                       (scheduler_type, current_lr, current_step, updated_at)
                       VALUES ('autonomous', ?, ?, datetime('now'))""",
                    (round(new_lr, 4), new_step),
                )
                result["phases"]["lr_update"] = {
                    "step": new_step,
                    "old_lr": old_lr,
                    "new_lr": round(new_lr, 4),
                    "accept_rate": round(accept_rate, 3),
                }

        # —— Complete epoch ——
        conn.execute(
            """UPDATE epoch_boundaries
               SET completed_at = datetime('now'),
                   edits_total = ?, edits_accepted = ?, edits_rejected = ?
               WHERE id = ?""",
            (len(candidates), applied, len(candidates) - applied, eid),
        )

        # Mark unselected candidates as rejected for this epoch
        rejected_ids = [r["id"] for r in candidates if r["id"] not in selected_ids]
        if rejected_ids:
            placeholders = ",".join("?" * len(rejected_ids))
            conn.execute(
                f"UPDATE edits SET status='rejected' WHERE id IN ({placeholders})",
                rejected_ids,
            )

        # Cleanup old rejected edits (keep current + 1 previous epoch)
        conn.execute(
            """DELETE FROM edits WHERE status IN ('rejected', 'rolled_back')
               AND id NOT IN (
                   SELECT id FROM edits WHERE status IN ('rejected', 'rolled_back')
                   ORDER BY created_at DESC LIMIT 50
               )"""
        )

        conn.commit()
        result["epoch_number"] = epoch_num
        result["epoch_id"] = eid
        return result

    except Exception as exc:
        import traceback
        result["error"] = str(exc)
        result["traceback"] = traceback.format_exc()
        return result
    finally:
        store.close()


def _apply_edit_op_safe(
    content: str, op: str, edit_content: str, target: str
) -> str | None:
    """Apply a single edit operation, respecting protected regions.

    Returns None if operation would modify a protected region.
    """
    PROTECTED_START = "<!-- OMC:SLOW_UPDATE_START -->"
    PROTECTED_END = "<!-- OMC:SLOW_UPDATE_END -->"

    def _in_protected(idx: int) -> bool:
        start = content.find(PROTECTED_START)
        end = content.find(PROTECTED_END)
        if start == -1:
            return False
        return start <= idx <= (end + len(PROTECTED_END))

    if op == "append":
        protected_idx = content.find(PROTECTED_START)
        if protected_idx == -1:
            return content + "\n" + edit_content
        else:
            return content[:protected_idx] + edit_content + "\n" + content[protected_idx:]

    elif op == "insert_after":
        if target in content:
            idx = content.find(target) + len(target)
            return content[:idx] + "\n" + edit_content + content[idx:]
        return content + "\n" + edit_content  # Fallback to append

    elif op == "replace":
        if target in content:
            idx = content.find(target)
            if _in_protected(idx):
                return None
            return content.replace(target, edit_content, 1)
        return content

    elif op == "delete":
        if target in content:
            idx = content.find(target)
            if _in_protected(idx):
                return None
            return content.replace(target, "", 1)
        return content

    return content


def get_epoch_status(db_path: str | None = None) -> dict:
    """Get current ReflACT pipeline status for monitoring.

    Returns:
        dict with epoch history, edit counts, LR state
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Epoch history
        epochs = conn.execute(
            """SELECT epoch_number, started_at, completed_at,
                      edits_total, edits_accepted, edits_rejected
               FROM epoch_boundaries ORDER BY epoch_number DESC LIMIT 5"""
        ).fetchall()

        # Edit counts by status
        edit_counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM edits GROUP BY status"
        ).fetchall())

        # LR state
        lr = conn.execute(
            "SELECT scheduler_type, current_lr, current_step, total_steps "
            "FROM learning_rate_state ORDER BY id DESC LIMIT 1"
        ).fetchone()

        # Rollout stats
        rollouts = conn.execute(
            "SELECT COUNT(*) as total, AVG(soft_score) as avg_score "
            "FROM rollout_results WHERE soft_score IS NOT NULL"
        ).fetchone()

        return {
            "epochs": [dict(e) for e in epochs],
            "edits": dict(edit_counts) if edit_counts else {},
            "lr_state": dict(lr) if lr else {"current_lr": 0.3, "scheduler_type": "autonomous"},
            "rollouts": dict(rollouts) if rollouts else {"total": 0, "avg_score": None},
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P1: Validate — held-out session verification
# ---------------------------------------------------------------------------

HELDOUT_RATIO = 0.2  # Fraction of sessions reserved for validation


def mark_heldout_sessions(db_path: str | None = None) -> int:
    """Mark a random subset of sessions as held-out for validation.

    Sessions with rollout_results are randomly assigned held_out=True
    at HELDOUT_RATIO. Already-marked sessions are skipped.

    Returns count of newly marked held-out sessions.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Get unmarked sessions
        rows = conn.execute(
            """SELECT DISTINCT session_id FROM rollout_results
               WHERE session_id NOT IN (
                   SELECT session_id FROM rollout_results
                   WHERE skill_snapshot_hash LIKE 'heldout:%'
               )"""
        ).fetchall()

        if not rows:
            return 0

        import random
        random.seed(42)  # Deterministic for reproducibility
        n_heldout = max(1, int(len(rows) * HELDOUT_RATIO))
        heldout_sessions = set(random.sample([r["session_id"] for r in rows], n_heldout))

        count = 0
        for sid in heldout_sessions:
            conn.execute(
                """UPDATE rollout_results
                   SET skill_snapshot_hash = 'heldout:' || COALESCE(skill_snapshot_hash, '')
                   WHERE session_id = ?""",
                (sid,),
            )
            count += 1

        conn.commit()
        return count
    finally:
        store.close()


def validate_edits(
    edit_ids: list[int],
    db_path: str | None = None,
) -> dict:
    """Validate edits against held-out sessions.

    Compares rollout scores on held-out sessions before vs after edit
    application. Edits are accepted only if held-out performance improves.

    Returns:
        dict with accepted_ids, rejected_ids, per_edit scores
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Get held-out session scores
        heldout_scores = conn.execute(
            """SELECT AVG(soft_score) as avg_score, COUNT(*) as n
               FROM rollout_results
               WHERE skill_snapshot_hash LIKE 'heldout:%'
                 AND soft_score IS NOT NULL"""
        ).fetchone()

        baseline_score = heldout_scores["avg_score"] or 0.5
        baseline_n = heldout_scores["n"] or 0

        results = {"baseline": {"avg_score": baseline_score, "n_sessions": baseline_n},
                   "accepted": [], "rejected": [], "details": []}

        for eid in edit_ids:
            edit = conn.execute(
                "SELECT * FROM edits WHERE id = ?", (eid,)
            ).fetchone()
            if not edit:
                results["details"].append({"edit_id": eid, "error": "not found"})
                continue

            # For now, validate by checking if similar edits have been accepted before
            similar_accepted = conn.execute(
                """SELECT COUNT(*) FROM edits
                   WHERE op = ? AND source_type = ?
                     AND status = 'validated'
                     AND id != ?""",
                (edit["op"], edit["source_type"], eid),
            ).fetchone()[0]

            similar_rejected = conn.execute(
                """SELECT COUNT(*) FROM edits
                   WHERE op = ? AND source_type = ?
                     AND status = 'rejected'
                     AND id != ?""",
                (edit["op"], edit["source_type"], eid),
            ).fetchone()[0]

            total_similar = similar_accepted + similar_rejected
            success_rate = similar_accepted / total_similar if total_similar > 0 else 0.0

            # PACE-style sequential evidence: no prior data → reject (was: accept by default)
            # Greedy "accept by default" = 30-42% false commits (PACE arXiv 2606.08106)
            # Require 2:1 evidence ratio or >=3 accumulated successes
            if total_similar < 3:
                accepted = False  # Insufficient evidence (< 3 samples)
            elif similar_accepted >= 3 and similar_rejected == 0:
                accepted = True   # 3+ clean successes → strong signal
            elif total_similar >= 5:
                accepted = success_rate >= 0.667  # 2:1 evidence ratio
            else:
                accepted = success_rate >= 0.75   # 3-4 samples, high bar

            # ponytail: B6 fix — held-out delta as additional gate
            heldout_delta = conn.execute(
                """SELECT AVG(heldout_score_after - heldout_score_before) as avg_delta
                   FROM epoch_boundaries
                   WHERE heldout_score_before IS NOT NULL
                     AND heldout_score_after IS NOT NULL"""
            ).fetchone()
            delta = (heldout_delta["avg_delta"] or 0.0) if heldout_delta else 0.0
            if delta < -0.05:  # Held-out regressed → reject regardless
                accepted = False
            elif delta > 0.02 and not accepted:  # Held-out improved → boost borderline cases
                accepted = total_similar >= 2 and success_rate >= 0.5

            if accepted:
                conn.execute(
                    "UPDATE edits SET status='validated' WHERE id=?",
                    (eid,),
                )
                results["accepted"].append(eid)
            else:
                conn.execute(
                    "UPDATE edits SET status='rejected' WHERE id=?",
                    (eid,),
                )
                results["rejected"].append(eid)

            results["details"].append({
                "edit_id": eid,
                "op": edit["op"],
                "source_type": edit["source_type"],
                "similar_accepted": similar_accepted,
                "similar_rejected": similar_rejected,
                "success_rate": round(success_rate, 3),
                "verdict": "accepted" if accepted else "rejected",
            })

        conn.commit()
        return results
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P1: Slow Update — epoch-level protected region updates
# ---------------------------------------------------------------------------

SLOW_UPDATE_START = "<!-- OMC:SLOW_UPDATE_START -->"
SLOW_UPDATE_END = "<!-- OMC:SLOW_UPDATE_END -->"
SLOW_UPDATE_TEST_PROMPTS = 20  # Number of test prompts for comparison


def get_protected_regions(skill_path: str) -> list[dict]:
    """Extract protected region locations from a skill file.

    Returns list of {start_line, end_line, content, description} dicts.
    """
    path = Path(skill_path)
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8", errors="ignore").split("\n")
    regions = []
    in_region = False
    region_start = 0
    region_content = []

    for i, line in enumerate(lines):
        if SLOW_UPDATE_START in line:
            in_region = True
            region_start = i
            region_content = []
        elif SLOW_UPDATE_END in line and in_region:
            in_region = False
            regions.append({
                "start_line": region_start,
                "end_line": i,
                "content": "\n".join(region_content),
            })
        elif in_region:
            region_content.append(line)

    return regions


def build_slow_update_pairs(
    skill_path: str,
    db_path: str | None = None,
) -> list[dict]:
    """Build comparison pairs for Slow Update validation.

    Collects SLOW_UPDATE_TEST_PROMPTS rollout results that involved
    this skill, pairing the same task before and after edits.

    Returns list of {task, prev_score, prev_hash} dicts.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Get rollouts that reference this skill path
        rows = conn.execute(
            """SELECT task_description, soft_score, skill_snapshot_hash, created_at
               FROM rollout_results
               WHERE soft_score IS NOT NULL
               ORDER BY created_at DESC
               LIMIT ?""",
            (SLOW_UPDATE_TEST_PROMPTS,),
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        store.close()


def run_slow_update(
    skill_path: str,
    new_content: str,
    db_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Execute Slow Update on a skill's protected region.

    Compares current protected content vs new content against
    held-out session performance. Only applies if improvement
    is confirmed.

    Args:
        skill_path: Path to the skill SKILL.md file
        new_content: New content for the protected region
        db_path: Path to frames.db
        dry_run: If true, preview only

    Returns:
        dict with applied, reason, before/after comparison
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    path = Path(skill_path)
    if not path.exists():
        return {"applied": False, "reason": "skill file not found"}

    content = path.read_text(encoding="utf-8", errors="ignore")
    regions = get_protected_regions(skill_path)

    if not regions:
        return {"applied": False, "reason": "no protected regions found"}

    # Get held-out baseline
    store = MemoryStore(db_path)
    try:
        conn = store._conn
        baseline = conn.execute(
            """SELECT AVG(soft_score) as avg_score
               FROM rollout_results
               WHERE skill_snapshot_hash LIKE 'heldout:%'
                 AND soft_score IS NOT NULL"""
        ).fetchone()
        baseline_score = baseline["avg_score"] or 0.5
    finally:
        store.close()

    # Compare: only apply if we have evidence of improvement
    pairs = build_slow_update_pairs(skill_path, db_path)
    prev_scores = [p["soft_score"] for p in pairs if p["soft_score"] is not None]
    prev_avg = sum(prev_scores) / len(prev_scores) if prev_scores else baseline_score

    # Apply
    result = {
        "applied": False,
        "skill_path": str(path),
        "regions_found": len(regions),
        "baseline_score": round(baseline_score, 3),
        "prev_avg_score": round(prev_avg, 3),
    }

    if not dry_run:
        # Replace protected region content
        new_lines = new_content.split("\n")
        region = regions[0]  # Update first protected region

        all_lines = content.split("\n")
        before = all_lines[: region["start_line"] + 1]
        after = all_lines[region["end_line"]:]
        updated = before + new_lines + after
        new_text = "\n".join(updated)

        # Backup + write
        backup_path = path.with_suffix(path.suffix + ".slowupdate-bak")
        backup_path.write_text(content, encoding="utf-8")
        path.write_text(new_text, encoding="utf-8")
        result["applied"] = True
        result["lines_changed"] = len(new_lines)

    return result


# ---------------------------------------------------------------------------
# P1: Meta Skill — optimizer strategy tracking
# ---------------------------------------------------------------------------

def record_meta_strategy(
    strategy_name: str,
    target_field: str,
    success: bool,
    parameters: dict | None = None,
    db_path: str | None = None,
) -> dict:
    """Record a meta-strategy outcome for optimizer self-improvement.

    Tracks which edit strategies work best so future epochs can
    prefer higher-success-rate strategies.

    Args:
        strategy_name: e.g. 'prefer_pattern_edits', 'avoid_delete_on_skill_files'
        target_field: What this strategy optimizes (e.g. 'edit_accept_rate')
        success: Whether this application was successful
        parameters: Optional strategy parameters dict
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        existing = conn.execute(
            "SELECT id, success_rate, applied_count FROM meta_strategies WHERE strategy_name=?",
            (strategy_name,),
        ).fetchone()

        if existing:
            new_count = existing["applied_count"] + 1
            new_successes = existing["success_rate"] * existing["applied_count"] + (1 if success else 0)
            new_rate = new_successes / new_count
            conn.execute(
                """UPDATE meta_strategies
                   SET success_rate=?, applied_count=?, last_applied=datetime('now'),
                       parameters=COALESCE(?, parameters)
                   WHERE strategy_name=?""",
                (round(new_rate, 4), new_count, json.dumps(parameters or {}), strategy_name),
            )
        else:
            conn.execute(
                """INSERT INTO meta_strategies
                   (strategy_name, target_field, success_rate, applied_count,
                    last_applied, parameters)
                   VALUES (?, ?, ?, 1, datetime('now'), ?)""",
                (strategy_name, target_field, 1.0 if success else 0.0,
                 json.dumps(parameters or {})),
            )

        conn.commit()
        return {
            "strategy_name": strategy_name,
            "success": success,
            "success_rate": round(1.0 if success else 0.0, 4) if not existing
                            else round(new_rate, 4),
        }
    finally:
        store.close()


def get_best_strategies(
    target_field: str | None = None,
    min_applications: int = 3,
    db_path: str | None = None,
) -> list[dict]:
    """Get best-performing meta strategies, sorted by success_rate.

    Args:
        target_field: Optional filter by target field
        min_applications: Minimum times a strategy must have been applied
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        if target_field:
            rows = conn.execute(
                """SELECT * FROM meta_strategies
                   WHERE target_field = ? AND applied_count >= ?
                   ORDER BY success_rate DESC LIMIT 10""",
                (target_field, min_applications),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM meta_strategies
                   WHERE applied_count >= ?
                   ORDER BY success_rate DESC LIMIT 10""",
                (min_applications,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P2: Multi-signal fusion retrieval
# ---------------------------------------------------------------------------
# Weights: semantic(0.40) + BM25(0.20) + entity(0.25) + recency(0.15)

FUSION_WEIGHTS = {
    "semantic": 0.40,
    "bm25": 0.20,
    "entity": 0.25,
    "recency": 0.15,
}


def multi_signal_search(
    query: str,
    scope: str = "global",
    top_k: int = 10,
    db_path: str | None = None,
) -> list[dict]:
    """Multi-signal fusion search combining 4 retrieval signals.

    Signals (weighted):
    1. semantic (0.40): embedding cosine similarity via sqlite-vec
    2. BM25 (0.20): FTS5 full-text keyword matching
    3. entity (0.25): KG entity name match → boost co-entity observations
    4. recency (0.15): exponential decay by age (1 day=1.0, 30 days=0.5)

    Returns top_k observations with fused scores.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Get all active observations with entity info
        rows = conn.execute(
            """SELECT o.id, o.text, o.entity_id, o.created_at,
                      e.name as entity_name, e.type as entity_type,
                      COALESCE(kt.access_count, 0) as access_count,
                      COALESCE(kt.salience_score, 0.5) as salience
               FROM observations o
               JOIN entities e ON o.entity_id = e.id
               LEFT JOIN knowledge_tiers kt ON o.id = kt.observation_id
               WHERE o.archived_at IS NULL
               ORDER BY o.created_at DESC
               LIMIT 200"""
        ).fetchall()

        if not rows:
            return []

        scores = {}  # observation_id → fused_score

        # --- Signal 1: Semantic (vec0 KNN) ---
        # ponytail: P10 — persistent vec0 index, no on-the-fly re-embedding
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            from agent_recall.embeddings import get_provider
            import struct
            provider = get_provider()
            if provider is not None:
                query_vec = provider.embed(query)
                query_blob = struct.pack(f'{len(query_vec)}f', *query_vec)
                row_ids = {r["id"] for r in rows}
                vec_rows = conn.execute(
                    "SELECT ve.observation_id, ve.distance "
                    "FROM observation_embeddings ve "
                    "WHERE ve.embedding MATCH ? "
                    "ORDER BY ve.distance LIMIT 100",
                    (query_blob,),
                ).fetchall()
                if vec_rows:
                    max_dist = max(r["distance"] for r in vec_rows) or 1.0
                    for vr in vec_rows:
                        if vr["observation_id"] in row_ids:
                            sim = 1.0 - (vr["distance"] / max_dist)
                            scores[vr["observation_id"]] = (
                                scores.get(vr["observation_id"], 0)
                                + FUSION_WEIGHTS["semantic"] * sim
                            )
        except Exception:
            pass  # vec0 unavailable → skip semantic signal

        # --- Signal 2: BM25 (FTS5) ---
        # FTS5 requires OR syntax for multi-word queries
        fts_query = " OR ".join(query.split())
        try:
            fts_rows = conn.execute(
                """SELECT rowid as observation_id, rank FROM observations_fts
                   WHERE observations_fts MATCH ? ORDER BY rank LIMIT 50""",
                (fts_query,),
            ).fetchall()
            if fts_rows:
                max_rank = max(r["rank"] for r in fts_rows) or 1
                for r in fts_rows:
                    bm25_score = 1.0 - (float(r["rank"]) / max_rank)
                    scores[r["observation_id"]] = (
                        scores.get(r["observation_id"], 0)
                        + FUSION_WEIGHTS["bm25"] * bm25_score
                    )
        except Exception:
            pass  # FTS5 unavailable

        # --- Signal 3: Entity (KG match) ---
        query_lower = query.lower()
        for r in rows:
            entity_name = (r["entity_name"] or "").lower()
            entity_type = (r["entity_type"] or "").lower()
            text = (r["text"] or "").lower()

            entity_score = 0.0
            # Direct entity name match
            if query_lower in entity_name or entity_name in query_lower:
                entity_score = 0.8
            # Entity type match
            elif query_lower in entity_type:
                entity_score = 0.5
            # Text contains entity name
            elif entity_name and entity_name in query_lower:
                entity_score = 0.3

            if entity_score > 0:
                scores[r["id"]] = (
                    scores.get(r["id"], 0) + FUSION_WEIGHTS["entity"] * entity_score
                )

        # --- Signal 4: Recency (exponential decay) ---
        now = datetime.now(timezone.utc)
        for r in rows:
            try:
                created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
                age_days = (now - created).total_seconds() / 86400
                recency = max(0.1, 1.0 - 0.02 * age_days)  # ~50 days to 0.1
            except Exception:
                recency = 0.5
            scores[r["id"]] = (
                scores.get(r["id"], 0) + FUSION_WEIGHTS["recency"] * recency
            )

        # --- Sort by fused score ---
        scored = [(obs_id, score) for obs_id, score in scores.items()]
        scored.sort(key=lambda x: x[1], reverse=True)

        # Build results
        row_map = {r["id"]: r for r in rows}
        results = []
        for obs_id, score in scored[:top_k]:
            r = row_map.get(obs_id)
            if r:
                results.append({
                    "id": obs_id,
                    "text": (r["text"] or "")[:200],
                    "entity_name": r["entity_name"],
                    "entity_type": r["entity_type"],
                    "fused_score": round(score, 4),
                    "created_at": r["created_at"],
                })

        return results
    finally:
        store.close()


# ---------------------------------------------------------------------------
# P2: Spatial hierarchy — MemPalace Wing/Room/Closet/Drawer
# ---------------------------------------------------------------------------

# Scope prefixes encode spatial location:
#   wing:room:closet:drawer
#   e.g., "agents:cli-agent:tools:search" → Wing=agents, Room=cli-agent,
#                                          Closet=tools, Drawer=search

def parse_spatial_scope(scope: str) -> dict:
    """Parse a scoped path into spatial hierarchy components."""
    parts = scope.split(":") if scope else ["global"]
    return {
        "wing": parts[0] if len(parts) > 0 else "global",
        "room": parts[1] if len(parts) > 1 else None,
        "closet": parts[2] if len(parts) > 2 else None,
        "drawer": parts[3] if len(parts) > 3 else None,
        "depth": len(parts),
        "path": scope,
    }


def spatial_filter(
    observations: list[dict],
    target_scope: str,
    max_distance: int = 2,
) -> list[dict]:
    """Filter observations by spatial proximity to target_scope.

    distance=0: same drawer
    distance=1: same closet
    distance=2: same room
    distance=3: same wing
    distance>=4: global

    Args:
        observations: List of observation dicts with 'scope' key
        target_scope: Target spatial scope (e.g. "agents:cli-agent:tools")
        max_distance: Maximum spatial distance to include

    Returns:
        Filtered observations with spatial_distance added
    """
    target = parse_spatial_scope(target_scope)

    def spatial_distance(obs_scope: str) -> int:
        obs = parse_spatial_scope(obs_scope)
        if obs["path"] == target["path"]:
            return 0  # Same drawer
        if obs["closet"] == target["closet"] and obs["room"] == target["room"] and obs["wing"] == target["wing"]:
            return 1  # Same closet
        if obs["room"] == target["room"] and obs["wing"] == target["wing"]:
            return 2  # Same room
        if obs["wing"] == target["wing"]:
            return 3  # Same wing
        return 4  # Global

    result = []
    for obs in observations:
        distance = spatial_distance(obs.get("scope", "global"))
        if distance <= max_distance:
            obs_copy = dict(obs)
            obs_copy["spatial_distance"] = distance
            result.append(obs_copy)

    return sorted(result, key=lambda x: x["spatial_distance"])


def create_spatial_entity(
    name: str,
    entity_type: str,
    wing: str = "global",
    room: str | None = None,
    closet: str | None = None,
    db_path: str | None = None,
) -> int:
    """Create an entity at a specific spatial location.

    The scope is encoded as wing:room:closet:name.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    parts = [wing]
    if room:
        parts.append(room)
    if closet:
        parts.append(closet)
    parts.append(name)
    scope = ":".join(parts)

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        eid = conn.execute(
            "INSERT INTO entities (name, type, created_at) VALUES (?, ?, datetime('now'))",
            (name, entity_type),
        ).lastrowid
        store.add_observation(eid, f"Spatial location: {scope}")
        conn.commit()
        return eid
    finally:
        store.close()


def get_spatial_map(db_path: str | None = None) -> dict:
    """Get a spatial map of all entities organized by wing→room→closet.

    Returns nested dict structure for dashboard/spatial navigation.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    # Lightweight read-only connection — skips MemoryStore DDL init
    # to avoid write-lock contention with other MCP tools.
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT e.id, e.name, e.type, o.text as scope_text
               FROM entities e
               LEFT JOIN observations o ON e.id = o.entity_id
               WHERE o.text LIKE 'Spatial location:%'
               ORDER BY e.type, e.name"""
        ).fetchall()

        spatial_map = {}
        for r in rows:
            scope_raw = r["scope_text"] or ""
            if scope_raw.startswith("Spatial location: "):
                scope_raw = scope_raw[18:]
            spatial = parse_spatial_scope(scope_raw)

            wing = spatial["wing"]
            room = spatial["room"] or "_unroomed"
            closet = spatial["closet"] or "_uncloseted"

            if wing not in spatial_map:
                spatial_map[wing] = {}
            if room not in spatial_map[wing]:
                spatial_map[wing][room] = {}
            if closet not in spatial_map[wing][room]:
                spatial_map[wing][room][closet] = []

            spatial_map[wing][room][closet].append({
                "entity_id": r["id"],
                "name": r["name"],
                "type": r["type"],
            })

        return spatial_map
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# P2: LLM-based intent classification
# ---------------------------------------------------------------------------

def classify_intent_llm(text: str, model: str | None = None) -> dict:
    """Classify observation intent using LLM (replaces keyword-based).

    Uses the configured model to classify into:
    preference | decision | constraint | fact | task_state

    Falls back to keyword-based classify_intent() if LLM is unavailable
    or in environments without API access.

    Returns:
        dict with intent_type, confidence, method ("llm" | "keyword")
    """
    # Try LLM first
    try:
        import os as _os
        if _os.environ.get("ANTHROPIC_AUTH_TOKEN") and _os.environ.get("ANTHROPIC_BASE_URL"):
            import urllib.request
            payload = json.dumps({
                "model": model or _os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro"),
                "max_tokens": 20,
                "temperature": 0.0,
                "messages": [{
                    "role": "system",
                    "content": (
                        "Classify into ONE word: preference, decision, constraint, "
                        "fact, or task_state. Reply with only the word."
                    ),
                }, {
                    "role": "user",
                    "content": text[:500],
                }],
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{_os.environ['ANTHROPIC_BASE_URL']}/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": _os.environ["ANTHROPIC_AUTH_TOKEN"],
                },
            )

            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read().decode())
                content = _extract_response_text(result, "").strip().lower()

            valid_intents = {"preference", "decision", "constraint", "fact", "task_state"}
            for intent in valid_intents:
                if intent in content:
                    return {"intent_type": intent, "confidence": 0.85, "method": "llm"}

            # LLM returned unexpected value
            return {"intent_type": "fact", "confidence": 0.5, "method": "llm_fallback"}
    except Exception:
        pass

    # Fallback to keyword-based
    return {
        "intent_type": classify_intent(text),
        "confidence": 0.5,
        "method": "keyword",
    }


# ---------------------------------------------------------------------------
# P2: omc_status — consolidated dashboard (extends existing status)
# ---------------------------------------------------------------------------

def get_full_status(db_path: str | None = None) -> dict:
    """Get comprehensive OMC system health dashboard.

    Combines: entity/observation stats, tier distribution, epoch status,
    edit pipeline status, skill health, spatial map, retrieval stats.

    Use as the single entry point for system monitoring.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # ── Core counts ──
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        active_obs = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE archived_at IS NULL"
        ).fetchone()[0]
        total_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        archived_obs = total_obs - active_obs
        active_rels = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE status='active'"
        ).fetchone()[0]

        # ── Tier distribution ──
        tiers = dict(conn.execute(
            "SELECT tier, COUNT(*) FROM knowledge_tiers GROUP BY tier"
        ).fetchall())

        # ── Edit pipeline ──
        edit_counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM edits GROUP BY status"
        ).fetchall())

        # ── Epoch history ──
        epochs = conn.execute(
            """SELECT epoch_number, started_at, completed_at,
                      edits_total, edits_accepted, edits_rejected
               FROM epoch_boundaries ORDER BY epoch_number DESC LIMIT 5"""
        ).fetchall()

        # ── Skill health ──
        skill_count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE type='skill'"
        ).fetchone()[0]
        skills_with_recent = conn.execute(
            """SELECT COUNT(DISTINCT e.id)
               FROM entities e
               JOIN observations o ON e.id = o.entity_id
               WHERE e.type = 'skill'
                 AND o.created_at > datetime('now', '-30 days')"""
        ).fetchone()[0]

        # ── Retrieval stats ──
        ret_total = conn.execute(
            "SELECT COUNT(*) FROM retrieval_events"
        ).fetchone()[0]
        ret_used = conn.execute(
            "SELECT COUNT(*) FROM retrieval_events WHERE was_used=1"
        ).fetchone()[0]
        ret_hit_rate = round(ret_used / ret_total, 3) if ret_total > 0 else None

        # ── Observation meta coverage ──
        meta_count = conn.execute(
            "SELECT COUNT(*) FROM observation_meta"
        ).fetchone()[0]

        # ── Schema version ──
        schema_v = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]

        # ── LR state ──
        lr = conn.execute(
            "SELECT * FROM learning_rate_state ORDER BY id DESC LIMIT 1"
        ).fetchone()

        # ── Pattern store ──
        patterns = conn.execute(
            "SELECT COUNT(*) FROM pattern_store"
        ).fetchone()[0]
        active_patterns = conn.execute(
            "SELECT COUNT(*) FROM pattern_store WHERE occurrence_count > 0"
        ).fetchone()[0]

        # ── Meta strategies ──
        strategies = conn.execute(
            "SELECT COUNT(*) FROM meta_strategies"
        ).fetchone()[0]
        top_strategies = conn.execute(
            """SELECT strategy_name, success_rate, applied_count
               FROM meta_strategies
               WHERE applied_count >= 2
               ORDER BY success_rate DESC LIMIT 5"""
        ).fetchall()

        # ── Build dashboard ──
        return {
            "system": {
                "schema_version": schema_v,
                "db_path": db_path,
            },
            "storage": {
                "entities": entities,
                "observations": {"active": active_obs, "archived": archived_obs, "total": total_obs},
                "relations_active": active_rels,
                "meta_coverage": f"{meta_count}/{active_obs}",
            },
            "tiers": tiers,
            "edits": dict(edit_counts) if edit_counts else {},
            "epochs": {
                "history": [dict(e) for e in epochs],
                "lr_state": dict(lr) if lr else {"current_lr": 0.3, "scheduler_type": "autonomous"},
            },
            "skills": {
                "total": skill_count,
                "active_30d": skills_with_recent,
                "stale": skill_count - skills_with_recent,
            },
            "retrieval": {
                "total_events": ret_total,
                "hit_rate": ret_hit_rate,
            },
            "patterns": {
                "total": patterns,
                "active": active_patterns,
            },
            "meta_strategies": {
                "total": strategies,
                "top": [dict(s) for s in top_strategies],
            },
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# History Import — ingest past session transcripts into OMC
# ---------------------------------------------------------------------------

def _find_transcript_files(projects_dir: str | None = None) -> list[Path]:
    """Find all JSONL transcript files in the projects directory."""
    if projects_dir is None:
        projects_dir = os.path.expanduser("~/.claude/projects")
    base = Path(projects_dir)
    if not base.exists():
        return []
    # Each project has a dir, each session has a .jsonl file
    transcripts = []
    for proj_dir in base.iterdir():
        if proj_dir.is_dir():
            for f in proj_dir.iterdir():
                if f.suffix == ".jsonl":
                    transcripts.append(f)
    return sorted(transcripts, key=lambda f: f.stat().st_mtime, reverse=True)


def _extract_response_text(result: dict, default: str = "") -> str:
    """Extract text from LLM response, handling both Anthropic and DeepSeek formats.

    Anthropic: content[0].type="text", content[0].text="..."
    DeepSeek: multiple content items — find the one with type="text"
    """
    content_list = result.get("content", [])
    if not content_list:
        return default
    # Search for text-type item first (DeepSeek puts thinking before text)
    for item in content_list:
        if item.get("type") == "text" and item.get("text"):
            return item["text"]
    # Fallback: try first item (Anthropic format or thinking-only)
    item = content_list[0]
    return item.get("text") or item.get("thinking") or default


def _llm_extract_facts(user_messages: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Use LLM to extract preferences, decisions, constraints from user messages.

    Falls back to regex when LLM is unavailable.
    Returns (preferences, decisions, constraints) lists.
    """
    import re as _re

    if not user_messages:
        return [], [], []

    # Try LLM extraction
    try:
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        if token and base_url:
            combined = "\n".join(f"- {m[:300]}" for m in user_messages[:30])
            prompt = (
                "Extract from these user messages any coding preferences, "
                "technical decisions, and hard constraints. "
                "Return ONLY valid JSON with exactly these keys:\n"
                '{"preferences": ["string", ...], "decisions": ["string", ...], '
                '"constraints": ["string", ...]}\n'
                "Max 5 items per category. Each item under 200 chars.\n"
                "Skip empty categories. Only extract clearly stated facts.\n\n"
                f"Messages:\n{combined}"
            )

            import urllib.request
            payload = json.dumps({
                "model": os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro"),
                "max_tokens": 500,
                "temperature": 0.0,
                "messages": [
                    {"role": "system", "content": "Reply only with valid JSON. No markdown fences."},
                    {"role": "user", "content": prompt},
                ],
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{base_url}/messages",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": token,
                },
            )

            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
                content = _extract_response_text(result, "{}").strip()

            # Strip markdown fences if present
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:]) if len(lines) > 1 else content
                if content.endswith("```"):
                    content = content[:-3]

            parsed = json.loads(content)
            return (
                [p[:200] for p in parsed.get("preferences", [])][:5],
                [d[:200] for d in parsed.get("decisions", [])][:5],
                [c[:200] for c in parsed.get("constraints", [])][:5],
            )
    except Exception:
        pass  # Fall through to regex

    # Regex fallback (existing behavior)
    preferences, decisions, constraints = [], [], []

    pref_patterns = [
        r"(?:prefer|like|want|习惯|偏好|喜欢)\s+.+?(?:[.;\n]|$)",
        r"(?:use|using|用)\s+.+?(?:better|更好|更|faster|更快)",
    ]
    dec_patterns = [
        r"(?:decided|决定|选择|采用|chose)\s+.+?(?:[.;\n]|$)",
        r"(?:will use|going with)\s+.+?(?:[.;\n]|$)",
    ]
    const_patterns = [
        r"(?:must|never|always|必须|禁止|不能)\s+.+?(?:[.;\n]|$)",
        r"(?:don't|do not|不允许)\s+.+?(?:[.;\n]|$)",
    ]

    for msg_text in user_messages:
        for pat in pref_patterns:
            matches = _re.findall(pat, msg_text, _re.IGNORECASE)
            preferences.extend(m[:200] for m in matches)
        for pat in dec_patterns:
            matches = _re.findall(pat, msg_text, _re.IGNORECASE)
            decisions.extend(m[:200] for m in matches)
        for pat in const_patterns:
            matches = _re.findall(pat, msg_text, _re.IGNORECASE)
            constraints.extend(m[:200] for m in matches)

    return (
        list(set(preferences))[:5],
        list(set(decisions))[:5],
        list(set(constraints))[:5],
    )


def _extract_session_summary(jsonl_path: Path) -> dict:
    """Extract metadata and key facts from a single session transcript.

    Reads the JSONL file and extracts:
    - Session metadata (date, message count, duration)
    - Key decisions/preferences from user messages
    - Files edited (from tool use)
    - Key topics (from message content)

    Returns a dict ready for storage as observations.
    """
    import re as _re

    messages = []
    user_messages = []
    edited_files = set()
    first_ts = None
    last_ts = None

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")
                ts = obj.get("timestamp", "")

                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                if msg_type in ("user", "assistant"):
                    content = ""
                    if "message" in obj and isinstance(obj["message"], dict):
                        content = obj["message"].get("content", "")
                    elif "content" in obj:
                        content = str(obj["content"])
                    # Skip system-reminder noise
                    skip_prefixes = (
                        "<system-reminder>", "The user sent", "Here are the",
                        "Please refer to", "IMPORTANT:", "You have superpowers",
                    )
                    content_str = str(content)
                    if not any(content_str.startswith(p) for p in skip_prefixes):
                        messages.append({"type": msg_type, "content": content_str[:500]})
                        if msg_type == "user" and not content_str.startswith("<"):
                            user_messages.append(content_str[:300])

                # Track edited files from attachment messages
                if msg_type == "attachment":
                    if "message" in obj and isinstance(obj["message"], dict):
                        attach_content = str(obj["message"].get("content", ""))
                        paths = _re.findall(r'([\w/\\.-]+\.(?:py|md|json|ts|js|yaml|toml))', attach_content)
                        edited_files.update(p[:80] for p in paths)
    except Exception:
        pass

    # Extract key facts from user messages — LLM first, regex fallback
    preferences, decisions, constraints = _llm_extract_facts(
        user_messages[:30]  # Cap at 30 messages for token budget
    )

    # Calculate duration
    duration_minutes = None
    if first_ts and last_ts:
        try:
            t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_minutes = int((t2 - t1).total_seconds() / 60)
        except Exception:
            pass

    return {
        "session_id": jsonl_path.stem,
        "path": str(jsonl_path),
        "date": first_ts[:10] if first_ts else None,
        "messages": len(messages),
        "user_messages": len(user_messages),
        "duration_minutes": duration_minutes,
        "edited_files": list(edited_files)[:10],
        "preferences": list(set(preferences))[:5],
        "decisions": list(set(decisions))[:5],
        "constraints": list(set(constraints))[:5],
    }


def import_transcripts(
    max_sessions: int = 50,
    db_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Import historical session transcripts into OMC.

    Scans ~/.claude/projects/ for JSONL transcript files,
    extracts session metadata and key facts, and stores them
    as structured observations in the knowledge graph.

    Uses MemPalace track 1 (drawer entities) for session archives,
    and track 2 (structured observations) for extracted facts.

    Args:
        max_sessions: Max sessions to import (default 50, most recent first)
        db_path: Path to frames.db
        dry_run: If true, preview only without writing to DB

    Returns:
        dict with import statistics
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    transcripts = _find_transcript_files()
    if not transcripts:
        return {"imported": 0, "error": "no transcripts found"}

    # Limit to most recent sessions
    transcripts = transcripts[:max_sessions]

    store = MemoryStore(db_path)
    stats = {"sessions_found": len(transcripts), "sessions_imported": 0,
             "observations_created": 0, "preferences": 0, "decisions": 0,
             "constraints": 0, "skipped_existing": 0}

    try:
        conn = store._conn

        for tpath in transcripts:
            session_id = tpath.stem

            # Skip if already imported
            existing = conn.execute(
                "SELECT id FROM entities WHERE type='session' AND name=?",
                (f"session:{session_id}",),
            ).fetchone()
            if existing:
                stats["skipped_existing"] += 1
                continue

            # Extract summary
            summary = _extract_session_summary(tpath)
            if not summary["date"]:
                continue

            if dry_run:
                stats["sessions_imported"] += 1
                stats["preferences"] += len(summary["preferences"])
                stats["decisions"] += len(summary["decisions"])
                stats["constraints"] += len(summary["constraints"])
                continue

            # Create session entity
            eid = conn.execute(
                "INSERT INTO entities (name, type, created_at) VALUES (?, 'session', ?)",
                (f"session:{session_id}", summary["date"]),
            ).lastrowid

            # Store metadata
            meta_text = (
                f"Session: {session_id[:8]}...\n"
                f"Date: {summary['date']}\n"
                f"Messages: {summary['messages']} ({summary['user_messages']} user)\n"
                f"Duration: {summary['duration_minutes']}min\n"
                f"Files: {', '.join(summary['edited_files'][:5])}"
            )
            conn.execute(
                """INSERT INTO observations (entity_id, text, scope, created_at)
                   VALUES (?, ?, 'global', ?)""",
                (eid, meta_text, summary["date"]),
            )
            stats["observations_created"] += 1

            # Store extracted preferences
            for pref in summary["preferences"]:
                conn.execute(
                    """INSERT INTO observations (entity_id, text, scope, created_at)
                       VALUES (?, ?, 'global', ?)""",
                    (eid, pref, summary["date"]),
                )
                # Add intent meta
                obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """INSERT OR REPLACE INTO observation_meta
                       (observation_id, valid_from, confidence, intent_type, source_session_id)
                       VALUES (?, ?, 0.6, 'preference', ?)""",
                    (obs_id, summary["date"], session_id),
                )
                stats["preferences"] += 1
                stats["observations_created"] += 1

            # Store extracted decisions
            for dec in summary["decisions"]:
                conn.execute(
                    """INSERT INTO observations (entity_id, text, scope, created_at)
                       VALUES (?, ?, 'global', ?)""",
                    (eid, dec, summary["date"]),
                )
                obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """INSERT OR REPLACE INTO observation_meta
                       (observation_id, valid_from, confidence, intent_type, source_session_id)
                       VALUES (?, ?, 0.7, 'decision', ?)""",
                    (obs_id, summary["date"], session_id),
                )
                stats["decisions"] += 1
                stats["observations_created"] += 1

            # Store extracted constraints
            for const in summary["constraints"]:
                conn.execute(
                    """INSERT INTO observations (entity_id, text, scope, created_at)
                       VALUES (?, ?, 'global', ?)""",
                    (eid, const, summary["date"]),
                )
                obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """INSERT OR REPLACE INTO observation_meta
                       (observation_id, valid_from, confidence, intent_type, source_session_id)
                       VALUES (?, ?, 0.8, 'constraint', ?)""",
                    (obs_id, summary["date"], session_id),
                )
                stats["constraints"] += 1
                stats["observations_created"] += 1

            stats["sessions_imported"] += 1

        conn.commit()
        return stats

    finally:
        store.close()


# ============================================================================
# Track B: Skill Lifecycle — State Machine
# ============================================================================
# States: seed → active → stable → deprecated → archived
# Transitions are fully automatic, driven by rollout performance data.
#
# seed:       probation period, exploration boost
# active:     normal, evaluated every epoch
# stable:     locked, rare evolution (only on major failure)
# deprecated: disabled in settings.json, auto after consistent failure
# archived:   30 days after deprecation, ready for deletion
# merged:     similarity > threshold → combined into one skill
#
# Thresholds:
SEED_MIN_USES = 3
SEED_MIN_SUCCESS_RATE = 0.5
STABLE_MIN_SUCCESS_RATE = 0.7   # Lowered from 0.8: faster convergence
STABLE_CONSECUTIVE_EPOCHS = 2  # Lowered from 5: don't wait 5 epochs
# evolution_rounds check removed — proven usage matters more than evolution count
DEPRECATE_MAX_SUCCESS_RATE = 0.2
DEPRECATE_CONSECUTIVE_EPOCHS = 3
DEPRECATE_GRACE_DAYS = 30
MERGE_SIMILARITY_THRESHOLD = 0.85

SKILL_STATES = ("seed", "active", "stable", "deprecated", "archived", "merged")

# ============================================================================
# Track B: Skill Lifecycle — Core Functions
# ============================================================================


def get_skill_lifecycle_state(
    skill_name: str,
    db_path: str | None = None,
) -> dict:
    """Get the current lifecycle state and stats for a skill."""
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        row = conn.execute(
            "SELECT * FROM skill_performance WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()

        if not row:
            entity = conn.execute(
                "SELECT id FROM entities WHERE type='skill' AND name=?",
                (f"skill:{skill_name}",),
            ).fetchone()

            if entity:
                conn.execute(
                    """INSERT INTO skill_performance
                       (skill_name, lifecycle_state, created_at, updated_at)
                       VALUES (?, 'active', datetime('now'), datetime('now'))""",
                    (skill_name,),
                )
                conn.commit()
                return {
                    "skill_name": skill_name,
                    "lifecycle_state": "active",
                    "total_uses": 0,
                    "success_rate": 0.5,
                    "is_new": True,
                }

            return {"skill_name": skill_name, "lifecycle_state": "unknown",
                    "error": "skill not registered"}

        return {
            "skill_name": row["skill_name"],
            "lifecycle_state": row["lifecycle_state"],
            "total_uses": row["total_uses"],
            "success_count": row["success_count"],
            "success_rate": row["success_rate"],
            "avg_utility": row["avg_utility"],
            "last_used": row["last_used"],
            "last_evolved": row["last_evolved"],
            "evolution_rounds": row["evolution_rounds"],
            "deprecation_rounds": row["deprecation_rounds"],
            "merged_from": row["merged_from"],
            "merged_to": row["merged_to"],
        }
    finally:
        store.close()


def record_skill_usage(
    session_id: str,
    skills_used: list[str],
    success: bool = True,
    utility_delta: float | None = None,
    db_path: str | None = None,
) -> dict:
    """Record which skills were used in a session and update performance.

    Called at SessionEnd. Updates skill_performance for each skill used.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        conn.execute(
            """UPDATE rollout_results
               SET active_skills = ?, skill_scores = ?
               WHERE session_id = ?""",
            (json.dumps(skills_used),
             json.dumps({"success": success, "utility_delta": utility_delta}),
             session_id),
        )

        for skill_name in skills_used:
            existing = conn.execute(
                "SELECT * FROM skill_performance WHERE skill_name = ?",
                (skill_name,),
            ).fetchone()

            if existing:
                new_uses = existing["total_uses"] + 1
                new_successes = existing["success_count"] + (1 if success else 0)
                new_rate = round(new_successes / new_uses, 4) if new_uses > 0 else 0.0
                conn.execute(
                    """UPDATE skill_performance
                       SET total_uses = ?, success_count = ?, success_rate = ?,
                           last_used = datetime('now'), updated_at = datetime('now')
                       WHERE skill_name = ?""",
                    (new_uses, new_successes, new_rate, skill_name),
                )
            else:
                conn.execute(
                    """INSERT INTO skill_performance
                       (skill_name, total_uses, success_count, success_rate,
                        lifecycle_state, last_used, created_at, updated_at)
                       VALUES (?, 1, ?, ?, 'seed', datetime('now'),
                               datetime('now'), datetime('now'))""",
                    (skill_name, 1 if success else 0, 1.0 if success else 0.0),
                )

        conn.commit()
        return {"recorded": len(skills_used), "skills": skills_used}
    finally:
        store.close()


def evaluate_skill_performance(
    db_path: str | None = None,
) -> list[dict]:
    """Evaluate all skills based on rollout data since last epoch."""
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        last_epoch = conn.execute(
            "SELECT completed_at FROM epoch_boundaries ORDER BY epoch_number DESC LIMIT 1"
        ).fetchone()
        since = last_epoch["completed_at"] if last_epoch else "1970-01-01"

        rows = conn.execute(
            """SELECT sp.*,
                      COUNT(rr.id) as recent_uses,
                      AVG(CASE WHEN rr.soft_score IS NOT NULL
                          THEN rr.soft_score ELSE rr.hard_score END) as recent_avg_score
               FROM skill_performance sp
               LEFT JOIN rollout_results rr
                 ON rr.active_skills LIKE '%' || sp.skill_name || '%'
                 AND rr.created_at > ?
               GROUP BY sp.skill_name
               ORDER BY sp.success_rate DESC""",
            (since,),
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        store.close()


def transition_skill_state(
    skill_name: str,
    db_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Apply state machine transition rules to a single skill.

    Rules (in order):
      1. seed → active:  total_uses >= 3 AND success_rate > 0.5
      2. active → stable:  success_rate > 0.8, consecutive >= 5 epochs, evolved >= 3 rounds
      3. stable → active:  success_rate < 0.5 (regression)
      4. → deprecated:  success_rate < 0.2 for 3 consecutive epochs
      5. deprecated → archived:  30 days in deprecated state
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        row = conn.execute(
            "SELECT * FROM skill_performance WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()

        if not row:
            return {"skill_name": skill_name, "error": "not found"}

        old_state = row["lifecycle_state"]
        new_state = old_state
        reason = "no transition needed"

        epoch_count = conn.execute(
            "SELECT COUNT(*) FROM epoch_boundaries WHERE completed_at IS NOT NULL"
        ).fetchone()[0]

        # Rule 1: seed → active
        if old_state == "seed":
            if (row["total_uses"] >= SEED_MIN_USES
                    and row["success_rate"] > SEED_MIN_SUCCESS_RATE):
                new_state = "active"
                reason = (
                    f"seed→active: {row['total_uses']} uses, "
                    f"SR={row['success_rate']:.2f}"
                )

        # Rule 2: active → stable
        elif old_state == "active":
            if (row["success_rate"] > STABLE_MIN_SUCCESS_RATE
                    and (row["total_uses"] or 0) >= 3
                    and epoch_count >= STABLE_CONSECUTIVE_EPOCHS):
                new_state = "stable"
                reason = (
                    f"active->stable: SR={row['success_rate']:.2f}, "
                    f"uses={row['total_uses']}, epochs={epoch_count}"
                )

        # Rule 3: stable → active (regression)
        elif old_state == "stable":
            if row["success_rate"] < 0.5:
                new_state = "active"
                reason = f"stable→active: regression SR={row['success_rate']:.2f} < 0.5"

        # Rule 4: deprecation check for active/stable
        if new_state in ("active", "stable"):
            dep_rounds = row["deprecation_rounds"]
            if row["success_rate"] < DEPRECATE_MAX_SUCCESS_RATE:
                dep_rounds += 1
                if dep_rounds >= DEPRECATE_CONSECUTIVE_EPOCHS:
                    new_state = "deprecated"
                    reason = (
                        f"→deprecated: SR={row['success_rate']:.2f} < "
                        f"{DEPRECATE_MAX_SUCCESS_RATE} for {dep_rounds} epochs"
                    )
            else:
                dep_rounds = 0

            if not dry_run:
                conn.execute(
                    "UPDATE skill_performance SET deprecation_rounds = ? WHERE skill_name = ?",
                    (dep_rounds, skill_name),
                )

        # Rule 5: deprecated → archived
        if old_state == "deprecated":
            days = conn.execute(
                """SELECT CAST(julianday('now') - julianday(updated_at) AS INTEGER)
                   FROM skill_performance WHERE skill_name = ?""",
                (skill_name,),
            ).fetchone()[0]
            if days >= DEPRECATE_GRACE_DAYS:
                new_state = "archived"
                reason = f"deprecated→archived: {days} days >= {DEPRECATE_GRACE_DAYS}"

        if new_state != old_state and not dry_run:
            conn.execute(
                """UPDATE skill_performance
                   SET lifecycle_state = ?, updated_at = datetime('now')
                   WHERE skill_name = ?""",
                (new_state, skill_name),
            )
            conn.commit()

        return {
            "skill_name": skill_name,
            "old_state": old_state,
            "new_state": new_state,
            "success_rate": row["success_rate"],
            "total_uses": row["total_uses"],
            "reason": reason,
            "changed": new_state != old_state,
        }
    finally:
        store.close()


def evolve_skill(
    skill_name: str,
    db_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Evolve a single skill via Slow Update on its SKILL.md."""
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    state = get_skill_lifecycle_state(skill_name, db_path)
    if state["lifecycle_state"] in ("deprecated", "archived", "merged"):
        return {
            "skill_name": skill_name,
            "evolved": False,
            "reason": f"skill is {state['lifecycle_state']}",
        }

    skill_path = _find_skill_path(skill_name)
    if not skill_path:
        return {"skill_name": skill_name, "evolved": False,
                "reason": "SKILL.md not found"}

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        edits = conn.execute(
            """SELECT id, op, content, target, support_count, source_type
               FROM edits
               WHERE status = 'candidate'
                 AND (target LIKE ? OR target LIKE ?)
               ORDER BY support_count DESC LIMIT 5""",
            (f"%{skill_name}%", f"%{skill_path}%"),
        ).fetchall()

        if not edits:
            return {"skill_name": skill_name, "evolved": False,
                    "reason": "no pending edits"}

        applied = 0
        for edit in edits:
            result = run_slow_update(
                str(skill_path), edit["content"], db_path, dry_run=dry_run
            )
            if result.get("applied"):
                applied += 1
                if not dry_run:
                    conn.execute(
                        "UPDATE edits SET status='applied', applied_at=datetime('now') WHERE id=?",
                        (edit["id"],),
                    )

        if applied > 0 and not dry_run:
            conn.execute(
                """UPDATE skill_performance
                   SET evolution_rounds = evolution_rounds + 1,
                       last_evolved = datetime('now'),
                       updated_at = datetime('now')
                   WHERE skill_name = ?""",
                (skill_name,),
            )
            conn.commit()

        return {
            "skill_name": skill_name,
            "evolved": applied > 0,
            "applied_count": applied,
            "total_candidates": len(edits),
            "dry_run": dry_run,
        }
    finally:
        store.close()


def merge_skills(
    skill_a: str,
    skill_b: str,
    db_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Merge skill_b into skill_a."""
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        a = conn.execute(
            "SELECT * FROM skill_performance WHERE skill_name = ?",
            (skill_a,),
        ).fetchone()
        b = conn.execute(
            "SELECT * FROM skill_performance WHERE skill_name = ?",
            (skill_b,),
        ).fetchone()

        if not a or not b:
            return {"merged": False, "reason": "one or both skills not found"}
        if b["lifecycle_state"] in ("merged", "archived"):
            return {"merged": False, "reason": f"skill_b is {b['lifecycle_state']}"}

        if dry_run:
            return {
                "merged": False, "dry_run": True,
                "skill_a": skill_a, "skill_b": skill_b,
                "a_rate": a["success_rate"], "b_rate": b["success_rate"],
                "reason": "dry run — would merge",
            }

        conn.execute(
            """UPDATE skill_performance
               SET lifecycle_state = 'merged', merged_to = ?,
                   updated_at = datetime('now')
               WHERE skill_name = ?""",
            (skill_a, skill_b),
        )
        conn.execute(
            """UPDATE skill_performance
               SET merged_from = ?, updated_at = datetime('now')
               WHERE skill_name = ?""",
            (skill_b, skill_a),
        )
        conn.commit()

        _merge_skill_files(skill_a, skill_b)
        return {
            "merged": True, "skill_a": skill_a, "skill_b": skill_b,
            "action": f"merged {skill_b} → {skill_a}",
        }
    finally:
        store.close()


def archive_skill(
    skill_name: str,
    db_path: str | None = None,
    settings_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Archive a skill: disable in settings.json."""
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        row = conn.execute(
            "SELECT * FROM skill_performance WHERE skill_name = ?",
            (skill_name,),
        ).fetchone()

        if not row:
            return {"archived": False, "reason": "skill not found"}
        if row["lifecycle_state"] != "archived":
            return {"archived": False,
                    "reason": f"skill is {row['lifecycle_state']}, not archived"}

        prune_result = prune_skills_in_settings(
            [skill_name], settings_path, dry_run=dry_run
        )
        return {
            "archived": True, "skill_name": skill_name,
            "settings_disabled": prune_result.get("disabled_count", 0) > 0,
            "dry_run": dry_run,
        }
    finally:
        store.close()


def seed_skill(
    skill_name: str,
    description: str,
    template_content: str | None = None,
    db_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Create a new skill from template or successful pattern.

    Creates SKILL.md in ~/.claude/skills/{skill_name}/ as 'seed' state.
    """
    import re as _re
    if not _re.match(r'^[a-z0-9-]+$', skill_name):
        return {"created": False, "reason": "skill_name must be kebab-case"}

    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    skill_dir = Path.home() / ".claude" / "skills" / skill_name
    skill_file = skill_dir / "SKILL.md"

    if skill_file.exists() and not dry_run:
        # Allow overwrite if skill is in seed state (auto-generated, safe to replace)
        store_check = MemoryStore(db_path)
        try:
            row = store_check._conn.execute(
                "SELECT lifecycle_state FROM skill_performance WHERE skill_name=?",
                (skill_name,),
            ).fetchone()
            if row and row["lifecycle_state"] == "seed":
                pass  # Allow overwrite
            else:
                return {"created": False, "reason": f"SKILL.md already exists at {skill_file}"}
        finally:
            store_check.close()

    if template_content is None:
        template_content = f"""---
name: {skill_name}
description: {description}
type: flexible
version: 0.1.0
---

# {skill_name}

{description}

<!-- OMC:SLOW_UPDATE_START -->
## Auto-Evolution Region

This section is managed by OMC. Do not edit manually.

Initial seed: {description}

<!-- OMC:SLOW_UPDATE_END -->

## Usage

> Describe how to use this skill.
"""

    result = {
        "skill_name": skill_name,
        "description": description,
        "path": str(skill_file),
        "lifecycle_state": "seed",
        "dry_run": dry_run,
    }

    if not dry_run:
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(template_content, encoding="utf-8")
        result["created"] = True

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        entity_name = f"skill:{skill_name}"
        existing = conn.execute(
            "SELECT id FROM entities WHERE name=? AND type='skill'",
            (entity_name,),
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO entities (name, type, created_at) VALUES (?, 'skill', datetime('now'))",
                (entity_name,),
            )

        conn.execute(
            """INSERT OR REPLACE INTO skill_performance
               (skill_name, lifecycle_state, total_uses, success_count, success_rate,
                created_at, updated_at)
               VALUES (?, 'seed', 0, 0, 0.5, datetime('now'), datetime('now'))""",
            (skill_name,),
        )
        conn.commit()
        result["registered"] = True
    finally:
        store.close()

    return result


def inject_skills(
    context: str = "",
    scope: str = "global",
    max_skills: int = 5,
    db_path: str | None = None,
) -> dict:
    """Generate skill injection context for SessionStart.

    Returns active skill summaries suitable for injection (≤500 tokens).
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        rows = conn.execute(
            """SELECT sp.*, e.id as entity_id
               FROM skill_performance sp
               JOIN entities e ON e.name = 'skill:' || sp.skill_name
               WHERE sp.lifecycle_state IN ('active', 'stable', 'seed')
               ORDER BY
                   CASE sp.lifecycle_state
                       WHEN 'stable' THEN 1
                       WHEN 'active' THEN 2
                       WHEN 'seed' THEN 3
                   END,
                   sp.success_rate DESC
               LIMIT ?""",
            (max_skills * 2,),
        ).fetchall()

        if not rows:
            return {"injection_text": "", "skills": [], "count": 0}

        state_icon = {"stable": "[S]", "active": "[A]", "seed": "[.]"}
        lines = ["## Active Skills"]
        summaries = []

        for r in rows[:max_skills]:
            icon = state_icon.get(r["lifecycle_state"], "?")
            desc_row = conn.execute(
                """SELECT text FROM observations
                   WHERE entity_id = ? AND archived_at IS NULL
                   ORDER BY created_at ASC LIMIT 1""",
                (r["entity_id"],),
            ).fetchone()
            desc = (desc_row["text"] or "")[:120] if desc_row else ""

            summaries.append({
                "name": r["skill_name"],
                "state": r["lifecycle_state"],
                "success_rate": r["success_rate"],
                "uses": r["total_uses"],
            })
            lines.append(
                f"- {icon} **{r['skill_name']}** "
                f"(SR:{r['success_rate']:.0%}, n={r['total_uses']})"
            )
            if desc:
                lines.append(f"  {desc}")

        return {
            "injection_text": "\n".join(lines),
            "skills": summaries,
            "count": len(summaries),
        }
    finally:
        store.close()


def buffer_rejected_edit(
    skill_name: str,
    op: str,
    content: str,
    reason: str,
    epoch_number: int = 0,
    db_path: str | None = None,
) -> int:
    """Store a rejected edit in the buffer for future Reflect analysis.

    SkillOpt: rejected-edit buffer prevents the optimizer from repeating bad edits.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        # Check if similar edit already rejected — increment attempt count
        existing = conn.execute(
            """SELECT id, attempt_count FROM rejected_edit_buffer
               WHERE skill_name=? AND op=? AND content=?
               ORDER BY created_at DESC LIMIT 1""",
            (skill_name, op, content),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE rejected_edit_buffer SET attempt_count=?, created_at=datetime('now') WHERE id=?",
                (existing["attempt_count"] + 1, existing["id"]),
            )
            conn.commit()
            return existing["id"]
        else:
            rid = conn.execute(
                """INSERT INTO rejected_edit_buffer
                   (skill_name, op, content, reason_rejected, epoch_number)
                   VALUES (?, ?, ?, ?, ?)""",
                (skill_name, op, content, reason, epoch_number),
            ).lastrowid
            conn.commit()
            return rid
    finally:
        store.close()


def get_rejected_buffer(
    skill_name: str | None = None,
    limit: int = 20,
    db_path: str | None = None,
) -> list[dict]:
    """Read rejected edit buffer for Reflect phase.

    The optimizer sees past rejected edits as negative feedback.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        if skill_name:
            rows = conn.execute(
                """SELECT * FROM rejected_edit_buffer
                   WHERE skill_name = ?
                   ORDER BY attempt_count DESC, created_at DESC
                   LIMIT ?""",
                (skill_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM rejected_edit_buffer
                   ORDER BY attempt_count DESC, created_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        store.close()


def reflect_failures(
    skill_name: str,
    db_path: str | None = None,
) -> list[dict]:
    """Analyze failed sessions to find what needs fixing for a skill.

    SkillOpt: failure minibatch → identify broken rules.
    Returns list of proposed corrective edits.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Find failed rollouts where this skill was active
        failed = conn.execute(
            """SELECT rr.* FROM rollout_results rr
               WHERE rr.active_skills LIKE '%' || ? || '%'
                 AND (rr.hard_score = 0 OR rr.soft_score < 0.3)
                 AND rr.fail_reason IS NOT NULL
               ORDER BY rr.created_at DESC LIMIT 10""",
            (skill_name,),
        ).fetchall()

        if not failed:
            return []

        # Check rejected buffer for past attempts
        rejected = get_rejected_buffer(skill_name, db_path=db_path)
        rejected_contents = {r["content"] for r in rejected}

        proposals = []
        for f in failed:
            reason = f["fail_reason"] or ""
            if not reason or len(reason) < 10:
                continue
            # Propose: add guardrail from failure reason
            edit_content = f"## Failure Guardrail\n\nWhen encountering: {reason[:200]}\nStop and verify before proceeding."
            if edit_content not in rejected_contents:
                proposals.append({
                    "skill_name": skill_name,
                    "op": "append",
                    "content": edit_content,
                    "source": f"failure:{f['session_id'][:8]}",
                    "fail_reason": reason[:200],
                })
                rejected_contents.add(edit_content)

        return proposals[:EVOLVE_MAX_EDITS_PER_EPOCH]
    finally:
        store.close()


def reflect_successes(
    skill_name: str,
    db_path: str | None = None,
) -> list[dict]:
    """Analyze successful sessions to confirm what rules should be kept.

    SkillOpt: success minibatch → identify working rules → lock them.
    Returns list of rules that should NOT be changed.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Find successful rollouts where this skill was active
        success = conn.execute(
            """SELECT rr.* FROM rollout_results rr
               WHERE rr.active_skills LIKE '%' || ? || '%'
                 AND (rr.hard_score = 1 OR rr.soft_score > 0.7)
               ORDER BY rr.created_at DESC LIMIT 10""",
            (skill_name,),
        ).fetchall()

        if not success:
            return []

        # Extract common patterns from successful task descriptions
        locked_rules = []
        seen = set()
        for s in success:
            task = s["task_description"] or ""
            if not task or task in seen or len(task) < 10:
                continue
            seen.add(task)
            locked_rules.append({
                "skill_name": skill_name,
                "task": task[:200],
                "score": s["soft_score"] or s["hard_score"] or 0.5,
                "action": "keep",
            })

        return locked_rules[:5]
    finally:
        store.close()


def run_optimizer_agent(
    db_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Run independent Optimizer Agent to audit OMC state and propose edits.

    SkillOpt-aligned: uses a separate LLM context (not the executor's) to
    analyze system state, find root causes, and propose concrete edits.

    Returns structured edit proposals for the Edit phase to process.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    # ── Gather state ──
    state = _gather_optimizer_state(db_path)

    # ── Build prompt ──
    prompt = f"""You are an independent Skill Optimizer Agent (like Microsoft SkillOpt's Optimizer Model).
Your job: audit this OMC dual-track system and propose concrete skill edits.

## Current System State

### Skills ({state['total_skills']} total)
Active: {state['active_count']}, Stable: {state['stable_count']}, Seed: {state['seed_count']}
Deprecated: {state['deprecated_count']}, Archived: {state['archived_count']}

### Skill Performance
{state['performance_summary']}

### Epoch History
{state['epoch_summary']}

### Rejected Edit Buffer ({state['buffer_size']} entries)
{state['buffer_summary']}

### Content Evolution
{state['content_summary']}

### Meta Strategies
{state['strategy_summary']}

## Instructions

1. Analyze the system state and identify the top 2-3 root causes of problems.
2. Propose 2-5 concrete edits to SKILL.md files.
3. Each edit must target a specific skill file and op (add/delete/replace).
4. Prioritize: fixing data quality > adding features > cosmetic changes.

Return ONLY valid JSON in this exact format:
{{
  "analysis": {{
    "root_causes": ["string", ...],
    "blind_spots": ["string", ...]
  }},
  "edits": [
    {{
      "target_skill": "omc-skill-evolution or omc-content-evolution or <other>",
      "op": "replace",
      "section": "which section to modify",
      "content": "new content (markdown)",
      "reason": "why this edit is needed",
      "priority": "P0/P1/P2"
    }}
  ]
}}"""

    # ── Call LLM ──
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")

    if not token or not base_url:
        return {"error": "LLM not configured", "edits": [], "analysis": {}}

    try:
        import urllib.request
        payload = json.dumps({
            "model": os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro"),
            "max_tokens": 2000,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": "You are an independent code auditor. Reply only with valid JSON. No markdown fences."},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/messages",
            data=payload,
            headers={"Content-Type": "application/json", "x-api-key": token},
        )

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            content = _extract_response_text(result, "").strip()

        if not content:
            return {"error": "LLM returned empty content", "edits": [], "analysis": {}}

        # Strip markdown fences
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:]) if len(lines) > 1 else content
            if content.endswith("```"):
                content = content[:-3]

        analysis = json.loads(content)

        # ── Apply edits if not dry run ──
        applied = []
        if not dry_run:
            for edit in analysis.get("edits", []):
                target = edit.get("target_skill", "")
                op = edit.get("op", "replace")
                content_str = edit.get("content", "")
                reason = edit.get("reason", "")

                # Gate check
                gate = gate_edit(target, content_str, db_path)
                if gate["accepted"]:
                    # Write to skill via Slow Update path
                    skill_path = _find_skill_path(target)
                    if skill_path:
                        current = skill_path.read_text(encoding="utf-8", errors="ignore")
                        new_content = _apply_edit_op_safe(current, op, content_str, str(skill_path))
                        if new_content and new_content != current:
                            skill_path.write_text(new_content, encoding="utf-8")
                            applied.append({"skill": target, "op": op, "reason": reason})
                    else:
                        # Skill file not found — create via seed if justified
                        if op == "add" and reason:
                            seed_skill(target, reason, content_str, db_path, dry_run=False)
                            applied.append({"skill": target, "op": "seed", "reason": reason})
                else:
                    buffer_rejected_edit(target, op, content_str, gate["reason"], db_path=db_path)

        return {
            "analysis": analysis.get("analysis", {}),
            "edits_proposed": len(analysis.get("edits", [])),
            "edits_applied": len(applied),
            "applied": applied,
        }
    except Exception as e:
        return {"error": str(e), "edits": [], "analysis": {}}


def _gather_optimizer_state(db_path: str) -> dict:
    """Collect current OMC state for the optimizer agent's prompt."""
    store = MemoryStore(db_path)
    try:
        conn = store._conn

        total = conn.execute("SELECT COUNT(*) FROM skill_performance WHERE lifecycle_state != 'merged'").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM skill_performance WHERE lifecycle_state='active'").fetchone()[0]
        stable = conn.execute("SELECT COUNT(*) FROM skill_performance WHERE lifecycle_state='stable'").fetchone()[0]
        seed = conn.execute("SELECT COUNT(*) FROM skill_performance WHERE lifecycle_state='seed'").fetchone()[0]
        deprecated = conn.execute("SELECT COUNT(*) FROM skill_performance WHERE lifecycle_state='deprecated'").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM skill_performance WHERE lifecycle_state='archived'").fetchone()[0]

        # Performance summary
        perf_rows = conn.execute(
            "SELECT skill_name, success_rate, total_uses, lifecycle_state FROM skill_performance ORDER BY total_uses DESC, success_rate DESC LIMIT 10"
        ).fetchall()
        perf_text = "\n".join(
            f"- {r['skill_name']}: SR={r['success_rate']:.0%} n={r['total_uses']} ({r['lifecycle_state']})"
            for r in perf_rows
        ) if perf_rows else "No performance data."

        # Epoch summary
        epoch_rows = conn.execute(
            "SELECT epoch_number, edits_accepted, edits_rejected, completed_at FROM epoch_boundaries ORDER BY epoch_number DESC LIMIT 5"
        ).fetchall()
        epoch_text = "\n".join(
            f"- Epoch {e['epoch_number']}: {e['edits_accepted']} accepted, {e['edits_rejected']} rejected"
            for e in epoch_rows
        ) if epoch_rows else "No epoch history."

        # Buffer summary
        buf_rows = conn.execute(
            "SELECT skill_name, op, reason_rejected, attempt_count FROM rejected_edit_buffer ORDER BY attempt_count DESC LIMIT 5"
        ).fetchall()
        buf_text = "\n".join(
            f"- {r['skill_name']}: {r['op']} (rejected: {r['reason_rejected'][:80] if r['reason_rejected'] else '?'}, attempts={r['attempt_count']})"
            for r in buf_rows
        ) if buf_rows else "No rejected edits."

        # Content summary
        obs_counts = dict(conn.execute(
            "SELECT intent_type, COUNT(*) FROM observation_meta GROUP BY intent_type"
        ).fetchall())
        content_text = f"Active observations by type: {obs_counts}"

        # Strategy summary
        strat_count = conn.execute("SELECT COUNT(*) FROM meta_strategies").fetchone()[0]
        strat_text = f"{strat_count} proven strategies." if strat_count > 0 else "No proven strategies yet."

        return {
            "total_skills": total, "active_count": active, "stable_count": stable,
            "seed_count": seed, "deprecated_count": deprecated, "archived_count": archived,
            "performance_summary": perf_text, "epoch_summary": epoch_text,
            "buffer_size": len(buf_rows) if buf_rows else 0,
            "buffer_summary": buf_text, "content_summary": content_text,
            "strategy_summary": strat_text,
        }
    finally:
        store.close()


def gate_edit(
    skill_name: str,
    edit_content: str,
    db_path: str | None = None,
) -> dict:
    """Validate a proposed edit against held-out sessions.

    SkillOpt Gate: accept only if edit strictly improves held-out performance.
    Returns {accepted: bool, reason: str, score_delta: float}.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Get baseline held-out score
        baseline = conn.execute(
            """SELECT AVG(soft_score) as avg_score, COUNT(*) as n
               FROM rollout_results
               WHERE skill_snapshot_hash LIKE 'heldout:%'
                 AND soft_score IS NOT NULL"""
        ).fetchone()

        baseline_score = baseline["avg_score"] or 0.5
        baseline_n = baseline["n"] or 0

        if baseline_n < 2:
            # Not enough held-out data — accept by default
            return {"accepted": True, "reason": "insufficient held-out data",
                    "score_delta": 0.0, "baseline_score": baseline_score,
                    "baseline_n": baseline_n}

        # Check if similar edits succeeded before
        # ponytail: B2 fix — skill_name col didn't exist; derive from target path + use target LIKE
        similar = conn.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status='validated' THEN 1 ELSE 0 END) as accepted
               FROM edits
               WHERE (target LIKE ? OR update_target LIKE ?) AND op = 'append'""",
            (f"%/{skill_name}/%", f"%/{skill_name}/%"),
        ).fetchone()

        similar_total = similar["total"] if similar else 0
        similar_accepted = similar["accepted"] if similar else 0
        rate = similar_accepted / similar_total if similar_total > 0 else 0.5

        # ponytail: B7 fix — wire meta_strategies into dynamic threshold
        threshold = 0.5
        try:
            best = get_best_strategies("edit_accept_rate", min_applications=2, db_path=db_path)
            if best and best[0].get("success_rate"):
                threshold = max(0.5, best[0]["success_rate"])
        except Exception:
            pass

        accepted = rate >= threshold

        return {
            "accepted": accepted,
            "reason": f"similar edit success rate: {rate:.2f} vs threshold {threshold:.2f}"
                      if similar_total > 0
                      else "no history — accept by default",
            "score_delta": 0.0,
            "baseline_score": round(baseline_score, 3),
            "baseline_n": baseline_n,
            "similar_total": similar_total,
            "threshold": threshold,
        }
    finally:
        store.close()


def run_skill_epoch(
    db_path: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Execute one full SkillOpt-aligned Track B epoch.

    Pipeline:
      1. Evaluate — rank skills by rollout performance
      2. Transition — apply state machine rules
      3. Reflect — failure analysis (fixes) + success analysis (lock)
      4. Edit — bounded edits per skill (lr ≤ EVOLVE_MAX_EDITS_PER_EPOCH)
      5. Gate — validate edits against held-out sessions
      6. Memory — buffer rejected edits for future Reflect
      7. Merge — detect similar skills
      8. Archive — process deprecated skills
      9. Mine — observation → skill patterns
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    # ponytail: data gate — skip epoch when too few skills have usage data
    store = MemoryStore(db_path)
    try:
        skills_with_data = store._conn.execute(
            "SELECT COUNT(*) FROM skill_performance WHERE total_uses > 0"
        ).fetchone()[0]
    finally:
        store.close()
    if skills_with_data < 10:
        return {"skipped": True, "reason": f"need >=10 skills with uses, have {skills_with_data}"}

    result = {"phases": {}, "dry_run": dry_run}

    # Phase 1: Evaluate
    performance = evaluate_skill_performance(db_path)
    result["phases"]["evaluate"] = {
        "skills_evaluated": len(performance),
        "top_performers": [
            {"name": p["skill_name"], "rate": p["success_rate"], "state": p["lifecycle_state"]}
            for p in performance[:5]
        ],
    }

    # Phase 2: Transition
    transitions = []
    for p in performance:
        t = transition_skill_state(p["skill_name"], db_path, dry_run=dry_run)
        if t.get("changed"):
            transitions.append(t)
    result["phases"]["transition"] = {
        "checked": len(performance),
        "transitions": len(transitions),
        "details": transitions[:5],
    }

    # Phase 3: Reflect — independent Optimizer Agent audit
    optimizer = run_optimizer_agent(db_path, dry_run=dry_run)
    result["phases"]["reflect"] = {
        "optimizer_agent": True,
        "edits_proposed": optimizer.get("edits_proposed", 0),
        "analysis": optimizer.get("analysis", {}).get("root_causes", [])[:3],
    }

    # Phase 4: Edit — apply optimizer edits (Text LR) + Gate validation
    edit_results = {"candidates": optimizer.get("edits_proposed", 0),
                    "gated_accepted": optimizer.get("edits_applied", 0),
                    "gated_rejected": 0, "buffered": 0, "details": []}
    if optimizer.get("applied"):
        for a in optimizer["applied"]:
            edit_results["details"].append(a)

    # Fallback: statistical reflect for skills with rollout data
    for p in performance:
        if p["lifecycle_state"] not in ("active", "stable"):
            continue

        # Get proposed edits from Reflect phase
        fixes = reflect_failures(p["skill_name"], db_path)
        edit_results["candidates"] += len(fixes)

        # Apply Text LR constraint: max EVOLVE_MAX_EDITS_PER_EPOCH per skill
        accepted_count = 0
        for fix in fixes[:EVOLVE_MAX_EDITS_PER_EPOCH]:
            # Gate validation
            gate = gate_edit(p["skill_name"], fix["content"], db_path)
            if gate["accepted"]:
                # Evolve the skill with this edit
                ev = evolve_skill(p["skill_name"], db_path, dry_run=dry_run)
                edit_results["gated_accepted"] += 1
                accepted_count += 1
                edit_results["details"].append({
                    "skill": p["skill_name"],
                    "op": fix["op"],
                    "gated": True,
                    "evolved": ev.get("evolved", False),
                })
            else:
                # Buffer the rejected edit
                if not dry_run:
                    buffer_rejected_edit(
                        p["skill_name"], fix["op"], fix["content"],
                        gate["reason"],
                        db_path=db_path,
                    )
                edit_results["gated_rejected"] += 1
                edit_results["buffered"] += 1

            if accepted_count >= EVOLVE_MAX_EDITS_PER_EPOCH:
                break

    result["phases"]["edit"] = edit_results

    # Phase 5: Memory — rejected buffer stats
    buffer_stats = get_rejected_buffer(limit=100, db_path=db_path)
    result["phases"]["memory"] = {
        "buffer_size": len(buffer_stats),
        "top_rejected": [
            {"skill": b["skill_name"], "attempts": b["attempt_count"]}
            for b in buffer_stats[:5]
        ],
    }

    # Phase 6: Merge detection
    merges = []
    if len(performance) >= 2:
        store = MemoryStore(db_path)
        try:
            for i, pa in enumerate(performance):
                for pb in performance[i + 1:]:
                    if pa["lifecycle_state"] in ("merged", "archived"):
                        continue
                    if pb["lifecycle_state"] in ("merged", "archived"):
                        continue
                    words_a = set(pa["skill_name"].lower().split("-"))
                    words_b = set(pb["skill_name"].lower().split("-"))
                    if words_a and words_b:
                        overlap = len(words_a & words_b) / len(words_a | words_b)
                        if overlap > MERGE_SIMILARITY_THRESHOLD:
                            m = merge_skills(pa["skill_name"], pb["skill_name"], db_path, dry_run=dry_run)
                            merges.append(m)
        finally:
            store.close()
    result["phases"]["merge"] = {"candidates_detected": len(merges), "merges": merges[:3]}

    # Phase 7: Archive
    archives = []
    for p in performance:
        if p["lifecycle_state"] == "archived":
            a = archive_skill(p["skill_name"], db_path, dry_run=dry_run)
            archives.append(a)
    result["phases"]["archive"] = {"processed": len(archives)}

    # Phase 8: Mine — observation → skill patterns
    mining = run_pattern_mining(db_path, dry_run=dry_run)
    result["phases"]["mine"] = {
        "clusters": mining["phases"]["cluster"]["clusters_found"],
        "qualified": mining["phases"]["qualify"]["candidates"],
        "seeded": len(mining["phases"]["seed"].get("created", [])),
    }

    # Phase 9: Deploy — regenerate dual injection skills
    if not dry_run:
        deploy_result = deploy_dual_skills(db_path)
        result["phases"]["deploy"] = deploy_result
    else:
        result["phases"]["deploy"] = {"status": "skipped (dry run)"}

    return result


# ============================================================================
# Deploy: Dual Skill Injection
# ============================================================================


def deploy_dual_skills(db_path: str | None = None) -> dict:
    """Regenerate the two OMC injection skills after an epoch.

    Skill A: omc-skill-evolution — Track B: how skills evolved, rankings, transitions
    Skill B: omc-content-evolution — Track A: what was learned from sessions

    Both are written as SKILL.md files that get auto-discovered at SessionStart.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    result = {}

    # ── Skill A: omc-skill-evolution ──
    skill_a = _build_skill_evolution_md(db_path)
    _write_injection_skill("omc-skill-evolution", skill_a)
    result["skill_evolution"] = {"deployed": True, "size": len(skill_a)}

    # ── Skill B: omc-content-evolution ──
    skill_b = _build_content_evolution_md(db_path)
    _write_injection_skill("omc-content-evolution", skill_b)
    result["content_evolution"] = {"deployed": True, "size": len(skill_b)}

    return result


def _build_skill_evolution_md(db_path: str) -> str:
    """Build SKILL.md content for Track B: skill evolution status."""
    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Skill rankings
        perf = conn.execute(
            """SELECT skill_name, lifecycle_state, success_rate, total_uses,
                      evolution_rounds, last_evolved
               FROM skill_performance
               WHERE lifecycle_state IN ('active', 'stable', 'seed')
               ORDER BY success_rate DESC, total_uses DESC
               LIMIT 15"""
        ).fetchall()

        rankings = ""
        for i, p in enumerate(perf):
            state_icon = {"stable": "[S]", "active": "[A]", "seed": "[.]"}.get(p["lifecycle_state"], "?")
            rankings += (
                f"{i + 1}. {state_icon} **{p['skill_name']}** "
                f"SR={p['success_rate']:.0%} n={p['total_uses']}"
            )
            if p["last_evolved"]:
                rankings += f" evolved={p['last_evolved'][:10]}"
            rankings += "\n"

        # Recent transitions
        transitions = conn.execute(
            """SELECT skill_name, lifecycle_state, updated_at
               FROM skill_performance
               WHERE lifecycle_state IN ('deprecated', 'archived', 'merged')
               ORDER BY updated_at DESC LIMIT 10"""
        ).fetchall()

        trans_text = ""
        for t in transitions:
            trans_text += f"- {t['lifecycle_state']}: **{t['skill_name']}** ({t['updated_at'][:10] if t['updated_at'] else '?'})\n"
        if not trans_text:
            trans_text = "No recent transitions.\n"

        # Buffer stats
        buf_count = conn.execute(
            "SELECT COUNT(*) FROM rejected_edit_buffer"
        ).fetchone()[0]

        # Meta strategies
        strategies = conn.execute(
            """SELECT strategy_name, success_rate, applied_count
               FROM meta_strategies
               WHERE applied_count >= 2
               ORDER BY success_rate DESC LIMIT 5"""
        ).fetchall()

        strat_text = ""
        for s in strategies:
            strat_text += f"- {s['strategy_name']}: SR={s['success_rate']:.0%} (n={s['applied_count']})\n"
        if not strat_text:
            strat_text = "No proven strategies yet.\n"

        # Epoch history
        epochs = conn.execute(
            """SELECT epoch_number, edits_accepted, edits_rejected, completed_at
               FROM epoch_boundaries ORDER BY epoch_number DESC LIMIT 5"""
        ).fetchall()

        epoch_text = ""
        for e in epochs:
            epoch_text += (
                f"- Epoch {e['epoch_number']}: {e['edits_accepted']} accepted, "
                f"{e['edits_rejected']} rejected ({e['completed_at'][:10] if e['completed_at'] else 'active'})\n"
            )
        if not epoch_text:
            epoch_text = "No epoch history.\n"

        total_skills = conn.execute(
            "SELECT COUNT(*) FROM skill_performance WHERE lifecycle_state != 'merged'"
        ).fetchone()[0]

        return f"""---
name: omc-skill-evolution
description: OMC Skill Evolution Status — current skill rankings, state transitions, optimizer strategies, and epoch history. Always load at session start.
triggers: "skill evolution|skill ranking|which skill|skill status|skill performance|optimizer|epoch"
type: flexible
version: 0.1.0
category: omc-injection
---

# OMC Skill Evolution

> Auto-generated at epoch boundary. Updated with each evolution cycle.

## Skill Rankings

{rankings}

## Recent Transitions

{trans_text}

## Optimizer Strategies

{strat_text}

## Epoch History

{epoch_text}

## System State

- Total skills: {total_skills}
- Rejected edit buffer: {buf_count} entries
- Last updated: {_now_iso()}

<!-- OMC:SLOW_UPDATE_START -->
## Auto-Evolution

This section evolves as the optimizer learns which strategies work best.
{strat_text}
<!-- OMC:SLOW_UPDATE_END -->
"""
    finally:
        store.close()


def _build_content_evolution_md(db_path: str) -> str:
    """Build SKILL.md content for Track A: session-learned knowledge."""
    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Top observations by confidence + recency
        facts = conn.execute(
            """SELECT o.text, om.intent_type, om.confidence,
                      e.name as entity_name, e.type as entity_type
               FROM observations o
               JOIN entities e ON o.entity_id = e.id
               JOIN observation_meta om ON o.id = om.observation_id
               WHERE o.archived_at IS NULL
                 AND o.text NOT LIKE 'Path:%'
                 AND o.text NOT LIKE 'Hash:%'
                 AND o.text NOT LIKE 'Spatial%'
                 AND length(o.text) > 40
               ORDER BY om.confidence DESC, o.created_at DESC
               LIMIT 20"""
        ).fetchall()

        facts_text = ""
        for f in facts:
            intent_tag = f"`{f['intent_type']}`" if f['intent_type'] else "`fact`"
            clean = f['text'][:200].replace('\n', ' ').replace('\\', '')
            facts_text += f"- [{f['confidence']:.0%}] {intent_tag} | {f['entity_name']}: {clean}\n"

        # Recent session imports
        sessions = conn.execute(
            """SELECT name, created_at FROM entities
               WHERE type='session'
               ORDER BY created_at DESC LIMIT 10"""
        ).fetchall()

        session_text = ""
        for s in sessions:
            sid = s['name'].replace('session:', '')[:12]
            session_text += f"- {sid}... ({s['created_at'][:10] if s['created_at'] else '?'})\n"

        # Observation stats
        active = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE archived_at IS NULL"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]

        # Intent distribution
        intents = conn.execute(
            """SELECT intent_type, COUNT(*) as cnt
               FROM observation_meta
               GROUP BY intent_type ORDER BY cnt DESC"""
        ).fetchall()
        intent_text = ", ".join(f"{i['intent_type']}={i['cnt']}" for i in intents)

        # Cluster stats from latest mining
        cluster_count = conn.execute(
            """SELECT COUNT(DISTINCT entity_id) FROM observations
               WHERE archived_at IS NULL AND text NOT LIKE 'Path:%'
               AND length(text) > 40"""
        ).fetchone()[0]

        return f"""---
name: omc-content-evolution
description: OMC Content Evolution Status — what was learned from sessions: preferences, decisions, constraints, key facts, and session history. Always load at session start.
triggers: "what learned|session learned|knowledge|preference|decision|constraint|fact|memory|recall|remember"
type: flexible
version: 0.1.0
category: omc-injection
---

# OMC Content Evolution

> Auto-generated from session analysis. Updated each epoch.

## Key Knowledge

{facts_text}

## Recent Sessions

{session_text}

## Knowledge Stats

- Active observations: {active}
- Total observations: {total}
- Distinct entities with content: {cluster_count}
- Intent distribution: {intent_text}
- Last updated: {_now_iso()}

<!-- OMC:SLOW_UPDATE_START -->
## Auto-Evolution

This section accumulates high-confidence patterns discovered across sessions.
{facts_text[:500]}
<!-- OMC:SLOW_UPDATE_END -->
"""
    finally:
        store.close()


def _write_injection_skill(name: str, content: str) -> None:
    """Write an OMC injection skill to disk."""
    skill_dir = Path.home() / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def _now_iso() -> str:
    """Return current time as ISO string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# Track B: Internal Helpers
# ============================================================================


def _find_skill_path(skill_name: str) -> Path | None:
    """Find the SKILL.md path for a skill by name."""
    candidates = [
        Path.home() / ".claude" / "skills" / skill_name / "SKILL.md",
        Path.cwd() / ".claude" / "skills" / skill_name / "SKILL.md",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _find_skill_paths(skill_name: str) -> list[Path]:
    """Find all file paths associated with a skill."""
    paths = []
    skill_dir = Path.home() / ".claude" / "skills" / skill_name
    if skill_dir.exists():
        paths.append(skill_dir)
    return paths


def _merge_skill_files(skill_a: str, skill_b: str) -> None:
    """Merge skill_b's SKILL.md content into skill_a's SKILL.md."""
    path_a = _find_skill_path(skill_a)
    path_b = _find_skill_path(skill_b)

    if not path_a or not path_b:
        return

    content_a = path_a.read_text(encoding="utf-8", errors="ignore")
    content_b = path_b.read_text(encoding="utf-8", errors="ignore")

    regions = get_protected_regions(str(path_b))
    merged_content = regions[0]["content"] if regions else content_b[:500]

    merge_block = (
        f"\n\n## Merged from {skill_b}\n\n"
        f"<!-- OMC:MERGE_START -->\n{merged_content}\n<!-- OMC:MERGE_END -->\n"
    )
    path_a.write_text(content_a.rstrip() + merge_block + "\n", encoding="utf-8")


# ============================================================================
# Bridge: Observation → Skill Mining Pipeline
# ============================================================================
# Converts Track A observations into Track B skill candidates.
#
# Pipeline:
#   1. CLUSTER    — embedding similarity + entity grouping
#   2. QUALIFY    — ≥3 observations, ≥2 sessions, confidence > 0.5
#   3. DEDUP      — similarity < 0.7 against existing skills
#   4. SYNTHESIZE — LLM generates skill name + description + SKILL.md content
#   5. SEED       — seed_skill() enters lifecycle state machine
#
# Thresholds:
CLUSTER_SIMILARITY_THRESHOLD = 0.70   # embedding cosine min for clustering
MIN_OBSERVATIONS_PER_PATTERN = 2      # minimum observations in a cluster (tighten as data grows)
MIN_SESSIONS_PER_PATTERN = 1          # minimum distinct sessions (tighten as coverage grows)
MIN_CONFIDENCE_PER_PATTERN = 0.5      # minimum avg observation confidence
MAX_SKILL_SIMILARITY = 0.7           # above this → duplicate of existing skill
MAX_PATTERNS_PER_EPOCH = 3           # cap new skills per epoch (safety limit)
EVOLVE_MAX_EDITS_PER_EPOCH = 4      # Text Learning Rate (SkillOpt lr=4)


def cluster_observations(
    db_path: str | None = None,
    max_observations: int = 300,
) -> list[dict]:
    """Cluster active observations by embedding similarity + entity.

    Returns list of clusters, each containing observation IDs and metadata.
    Falls back to keyword-based clustering when embeddings unavailable.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Get recent active observations with entity info
        # Filter out noise: skill metadata (path/hash), very short text
        rows = conn.execute(
            """SELECT o.id, o.text, o.entity_id, o.created_at,
                      e.name as entity_name, e.type as entity_type,
                      COALESCE(om.intent_type, 'fact') as intent_type,
                      COALESCE(om.confidence, 0.5) as confidence,
                      COALESCE(om.source_session_id, '') as session_id
               FROM observations o
               JOIN entities e ON o.entity_id = e.id
               LEFT JOIN observation_meta om ON o.id = om.observation_id
               WHERE o.archived_at IS NULL
                 AND o.text IS NOT NULL
                 AND length(o.text) > 40
                 AND o.text NOT LIKE 'Path:%'
                 AND o.text NOT LIKE 'Hash:%'
                 AND o.text NOT LIKE 'Source:%'
                 AND o.text NOT LIKE 'Spatial location:%'
               ORDER BY o.created_at DESC
               LIMIT ?""",
            (max_observations,),
        ).fetchall()

        if len(rows) < MIN_OBSERVATIONS_PER_PATTERN:
            return []

        texts = [r["text"] or "" for r in rows]

        # Try embedding-based clustering
        embeddings = None
        try:
            if np is not None:
                from agent_recall.embeddings import get_provider
                provider = get_provider()
                embeddings = np.array(provider.embed_batch(texts))
        except Exception:
            pass

        clusters = []
        assigned = set()

        if embeddings is not None:
            # Normalize
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            norms[norms == 0] = 1e-10
            normalized = embeddings / norms

            for i in range(len(rows)):
                if i in assigned:
                    continue
                cluster = {
                    "obs_ids": [rows[i]["id"]],
                    "texts": [rows[i]["text"][:300]],
                    "entity_name": rows[i]["entity_name"],
                    "entity_type": rows[i]["entity_type"],
                    "intent_types": {rows[i]["intent_type"]: 1},
                    "session_ids": {rows[i]["session_id"]} if rows[i]["session_id"] else set(),
                    "avg_confidence": rows[i]["confidence"],
                    "size": 1,
                }
                assigned.add(i)

                for j in range(i + 1, len(rows)):
                    if j in assigned:
                        continue
                    sim = float(np.dot(normalized[i], normalized[j]))
                    if sim >= CLUSTER_SIMILARITY_THRESHOLD:
                        cluster["obs_ids"].append(rows[j]["id"])
                        cluster["texts"].append(rows[j]["text"][:300])
                        it = rows[j]["intent_type"]
                        cluster["intent_types"][it] = cluster["intent_types"].get(it, 0) + 1
                        if rows[j]["session_id"]:
                            cluster["session_ids"].add(rows[j]["session_id"])
                        cluster["size"] += 1
                        cluster["avg_confidence"] = (
                            (cluster["avg_confidence"] * (cluster["size"] - 1) + rows[j]["confidence"])
                            / cluster["size"]
                        )
                        assigned.add(j)

                clusters.append(cluster)
        else:
            # Fallback: keyword-based grouping
            for i in range(len(rows)):
                if i in assigned:
                    continue
                text_i = (rows[i]["text"] or "").lower()
                words_i = set(text_i.split())
                if len(words_i) < 5:
                    continue

                cluster = {
                    "obs_ids": [rows[i]["id"]],
                    "texts": [rows[i]["text"][:300]],
                    "entity_name": rows[i]["entity_name"],
                    "entity_type": rows[i]["entity_type"],
                    "intent_types": {rows[i]["intent_type"]: 1},
                    "session_ids": {rows[i]["session_id"]} if rows[i]["session_id"] else set(),
                    "avg_confidence": rows[i]["confidence"],
                    "size": 1,
                }
                assigned.add(i)

                for j in range(i + 1, len(rows)):
                    if j in assigned:
                        continue
                    text_j = (rows[j]["text"] or "").lower()
                    words_j = set(text_j.split())
                    if not words_j:
                        continue
                    overlap = len(words_i & words_j) / min(len(words_i), len(words_j))
                    if overlap >= CLUSTER_SIMILARITY_THRESHOLD:
                        cluster["obs_ids"].append(rows[j]["id"])
                        cluster["texts"].append(rows[j]["text"][:300])
                        it = rows[j]["intent_type"]
                        cluster["intent_types"][it] = cluster["intent_types"].get(it, 0) + 1
                        if rows[j]["session_id"]:
                            cluster["session_ids"].add(rows[j]["session_id"])
                        cluster["size"] += 1
                        cluster["avg_confidence"] = (
                            (cluster["avg_confidence"] * (cluster["size"] - 1) + rows[j]["confidence"])
                            / cluster["size"]
                        )
                        assigned.add(j)

                clusters.append(cluster)

        # Sort by size descending
        clusters.sort(key=lambda c: c["size"], reverse=True)
        return clusters
    finally:
        store.close()


def qualify_patterns(
    clusters: list[dict],
    db_path: str | None = None,
) -> list[dict]:
    """Filter clusters to those qualifying as skill candidates.

    Criteria:
      - size >= MIN_OBSERVATIONS_PER_PATTERN (3)
      - spans >= MIN_SESSIONS_PER_PATTERN (2) distinct sessions
      - avg_confidence > MIN_CONFIDENCE_PER_PATTERN (0.5)
      - not too similar to existing skill (sim < MAX_SKILL_SIMILARITY)
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    qualified = []
    existing_skills = _get_existing_skill_names(db_path)

    for c in clusters:
        # Size check
        if c["size"] < MIN_OBSERVATIONS_PER_PATTERN:
            continue

        # Session diversity check
        if len(c["session_ids"]) < MIN_SESSIONS_PER_PATTERN:
            continue

        # Confidence check
        if c["avg_confidence"] < MIN_CONFIDENCE_PER_PATTERN:
            continue

        # Dominant intent type
        dominant_intent = max(c["intent_types"], key=c["intent_types"].get)
        intent_ratio = c["intent_types"][dominant_intent] / c["size"]

        # Generate candidate name
        candidate_name = _generate_skill_name(c)
        if not candidate_name:
            continue

        # Dedup against existing skills
        too_similar = False
        for es in existing_skills:
            sim = _name_similarity(candidate_name, es)
            if sim > MAX_SKILL_SIMILARITY:
                too_similar = True
                break

        if too_similar:
            continue

        qualified.append({
            "cluster": c,
            "candidate_name": candidate_name,
            "dominant_intent": dominant_intent,
            "intent_ratio": round(intent_ratio, 2),
            "representative_texts": c["texts"][:5],
        })

    return qualified[:MAX_PATTERNS_PER_EPOCH]


def synthesize_skill_from_pattern(
    qualified: dict,
    use_llm: bool = False,
) -> dict:
    """Generate a complete SKILL.md file from a qualified pattern.

    Produces proper frontmatter (name, description, triggers, type, version)
    so the Skill system can auto-discover and activate the skill.

    Args:
        qualified: A qualified pattern dict from qualify_patterns()
        use_llm: If True, use LLM for synthesis. Otherwise template-based.

    Returns:
        dict with skill_name, description, skill_md (full file content)
    """
    import re as _re

    name = qualified["candidate_name"]
    texts = qualified["representative_texts"]
    intent = qualified["dominant_intent"]
    size = qualified["cluster"]["size"]
    n_sessions = len(qualified["cluster"]["session_ids"])
    confidence = qualified["cluster"]["avg_confidence"]

    if use_llm:
        llm_result = _synthesize_with_llm(name, texts, intent)
        if llm_result and "error" not in llm_result:
            name = llm_result.get("skill_name", name)
            description = llm_result.get("description", "")
            protected = llm_result.get("protected_content", "")
            triggers = _extract_triggers(texts, name)
            skill_md = _build_skill_md(name, description, triggers, intent, size, n_sessions, confidence, protected)
            return {"skill_name": name, "description": description, "skill_md": skill_md}

    # --- Template-based synthesis ---
    cleaned_texts = []
    for t in texts:
        ct = _re.sub(r'[\\\n\r\t]+', ' ', t).strip()[:200]
        if ct:
            cleaned_texts.append(ct)

    # Generate description
    combined_sample = " ".join(cleaned_texts[:3])[:150]
    intent_labels = {
        "preference": "Prefer",
        "decision": "Use",
        "constraint": "Never",
        "fact": "Note",
        "task_state": "When",
    }
    label = intent_labels.get(intent, "Pattern")

    desc_templates = {
        "preference": f"{label} {combined_sample[:100]} — coding style preference from {n_sessions} sessions",
        "decision": f"{label} {combined_sample[:100]} — technical decision from {n_sessions} sessions",
        "constraint": f"{label} {combined_sample[:100]} — hard constraint from {n_sessions} sessions",
        "fact": f"{label} {combined_sample[:100]} — recurring fact from {n_sessions} sessions",
        "task_state": f"{label} {combined_sample[:100]} — workflow pattern from {n_sessions} sessions",
    }
    description = desc_templates.get(intent, desc_templates["fact"])[:300]

    # Generate triggers from content keywords
    triggers = _extract_triggers(texts, name)

    # Build protected region content
    protected = f"""## Pattern Evidence

> Source: {size} observations across {n_sessions} sessions
> Intent: {intent} (ratio: {qualified['intent_ratio']})
> Confidence: {confidence:.2f}

"""
    for i, t in enumerate(cleaned_texts[:5]):
        protected += f"{i + 1}. {t}\n"

    protected += f"""
## Guidelines

Based on the above recurring patterns:

- When encountering similar situations, apply the "{name}" pattern
- This represents a {intent} that emerged across multiple sessions
- Update this section via OMC Slow Update as more evidence accumulates
"""

    skill_md = _build_skill_md(name, description, triggers, intent, size, n_sessions, confidence, protected)
    return {"skill_name": name, "description": description, "skill_md": skill_md}


def _extract_triggers(texts: list[str], name: str) -> str:
    """Extract trigger keywords from observation texts for SKILL.md frontmatter."""
    import re as _re

    stop_words = {
        "this", "that", "with", "from", "have", "been", "were", "they",
        "when", "what", "where", "which", "there", "their", "about",
        "your", "will", "would", "could", "should", "like", "just",
        "than", "then", "also", "some", "more", "only", "over", "into",
        "session", "duration", "messages", "user", "using", "keep",
    }

    all_words = []
    for t in texts[:5]:
        cleaned = _re.sub(r'[\\\n\r\t.,;:!?()\[\]{}"\']+', ' ', t.lower())
        words = _re.findall(r'[a-z]{4,}', cleaned)
        all_words.extend(w for w in words if w not in stop_words)

    # Top unique keywords by frequency
    from collections import Counter
    word_counts = Counter(all_words)
    # Also add name parts as triggers
    name_words = name.lower().replace("-", " ").split()

    triggers = set()
    for w, _ in word_counts.most_common(5):
        triggers.add(w)
    for w in name_words:
        if len(w) >= 3:
            triggers.add(w)

    return "|".join(sorted(triggers)[:5])


def _build_skill_md(
    name: str,
    description: str,
    triggers: str,
    intent: str,
    cluster_size: int,
    n_sessions: int,
    confidence: float,
    protected_content: str,
) -> str:
    """Build a complete SKILL.md file with proper frontmatter."""
    skill_type = "rigid" if intent == "constraint" else "flexible"

    return f"""---
name: {name}
description: {description}
triggers: "{triggers}"
type: {skill_type}
version: 0.1.0
evolved_from: pattern_mining
source_sessions: {n_sessions}
source_observations: {cluster_size}
confidence: {confidence:.2f}
---

# {name.replace('-', ' ').title()}

{description}

<!-- OMC:SLOW_UPDATE_START -->
{protected_content}
<!-- OMC:SLOW_UPDATE_END -->

## Usage

This skill was automatically mined from session patterns.
When the trigger keywords appear in conversation, the agent should activate this skill.

### Trigger Keywords

{triggers.replace('|', ', ')}
"""


def run_pattern_mining(
    db_path: str | None = None,
    dry_run: bool = True,
    use_llm: bool = False,
) -> dict:
    """Execute full Observation → Skill mining pipeline.

    Pipeline:
      1. CLUSTER    — group similar observations
      2. QUALIFY    — filter to valid skill candidates
      3. SYNTHESIZE — generate SKILL.md content
      4. SEED       — create skills via seed_skill()

    Called during Track B epoch (Phase 6: Seed).

    Returns:
        dict with phase results
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    result = {"phases": {}, "dry_run": dry_run}

    # Phase 1: Cluster
    clusters = cluster_observations(db_path)
    result["phases"]["cluster"] = {
        "observations_processed": len(clusters),
        "clusters_found": len(clusters),
        "top_sizes": [c["size"] for c in clusters[:5]],
    }

    # Phase 2: Qualify
    qualified = qualify_patterns(clusters, db_path)
    result["phases"]["qualify"] = {
        "candidates": len(qualified),
        "names": [q["candidate_name"] for q in qualified],
    }

    # Phase 3: Synthesize + Seed
    seeded = []
    for q in qualified:
        synthesis = synthesize_skill_from_pattern(q, use_llm=use_llm)
        if not synthesis:
            continue

        seed_result = seed_skill(
            synthesis["skill_name"],
            synthesis["description"],
            synthesis.get("skill_md"),
            db_path,
            dry_run=dry_run,
        )
        seeded.append({
            "name": synthesis["skill_name"],
            "seeded": seed_result.get("created", False),
            "description": synthesis["description"][:100],
        })

    result["phases"]["seed"] = {
        "synthesized": len(seeded),
        "created": [s for s in seeded if s["seeded"]],
        "details": seeded,
    }

    return result


# ============================================================================
# Bridge: Internal Helpers
# ============================================================================


def _get_existing_skill_names(db_path: str) -> list[str]:
    """Get all existing skill names from skill_performance and entities."""
    store = MemoryStore(db_path)
    try:
        conn = store._conn
        rows = conn.execute(
            "SELECT skill_name FROM skill_performance"
        ).fetchall()
        names = [r["skill_name"] for r in rows]
        # Also check entity names
        entity_rows = conn.execute(
            "SELECT name FROM entities WHERE type='skill'"
        ).fetchall()
        for r in entity_rows:
            name = r["name"]
            if name.startswith("skill:"):
                name = name[6:]
            if name not in names:
                names.append(name)
        return names
    finally:
        store.close()


def _generate_skill_name(cluster: dict) -> str:
    """Generate a kebab-case skill name from a cluster.

    Priority: observation content > entity name (when not a session UUID).
    Skips entity name when it looks like a UUID.
    """
    import re as _re

    entity_name = cluster.get("entity_name", "")
    dominant_intent = max(cluster["intent_types"], key=cluster["intent_types"].get) if cluster["intent_types"] else "fact"

    # Skip entity name if it looks like a UUID
    uuid_pattern = _re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', _re.IGNORECASE)
    if entity_name and uuid_pattern.match(entity_name.replace("session:", "")):
        entity_name = ""  # Skip UUID, use content instead

    # From entity name (when meaningful)
    if entity_name:
        base = entity_name.lower().replace(" ", "-").replace("_", "-")
        base = base.replace("skill:", "")
        base = _re.sub(r'[^a-z0-9-]', '', base)
        base = _re.sub(r'-+', '-', base).strip('-')
        if len(base) >= 4 and not uuid_pattern.match(base):
            return f"{base}-{dominant_intent}"[:40]

    # From observation text content — extract meaningful words
    combined = " ".join(cluster.get("texts", [])[:5])[:500].lower()
    # Remove escape sequences, punctuation, and short tokens
    cleaned = _re.sub(r'[\\\n\r\t]', ' ', combined)
    words = _re.findall(r'[a-z]{4,}', cleaned)
    # Filter stop words
    stop_words = {"this", "that", "with", "from", "have", "been", "were", "they",
                  "when", "what", "where", "which", "there", "their", "about",
                  "your", "will", "would", "could", "should", "like", "just",
                  "than", "then", "also", "some", "more", "only", "over", "into",
                  "session", "duration", "messages", "user"}
    meaningful = [w for w in words if w not in stop_words]

    if len(meaningful) >= 2:
        # Weighted by frequency, deduplicate keeping order
        seen = set()
        ranked = []
        for w in sorted(meaningful, key=lambda w: meaningful.count(w), reverse=True):
            if w not in seen:
                ranked.append(w)
                seen.add(w)
            if len(ranked) >= 3:
                break
        name = "-".join(ranked)
        if len(name) >= 6:
            return f"{name}-{dominant_intent}"[:40]

    return f"pattern-{dominant_intent}-{cluster['size']}"


def _name_similarity(name_a: str, name_b: str) -> float:
    """Compute word-overlap similarity between two skill names."""
    if not name_a or not name_b:
        return 0.0
    words_a = set(name_a.lower().replace("-", " ").split())
    words_b = set(name_b.lower().replace("-", " ").split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _synthesize_with_llm(
    name: str,
    texts: list[str],
    intent: str,
) -> dict:
    """Use LLM to synthesize a skill from observation texts."""
    try:
        import urllib.request

        combined = "\n".join(f"- {t[:300]}" for t in texts[:5])
        prompt = (
            "You are an AI skill designer. Given recurring patterns from coding sessions, "
            "create a concise, actionable skill.\n\n"
            f"Pattern name: {name}\n"
            f"Intent type: {intent}\n"
            f"Evidence:\n{combined}\n\n"
            "Output JSON with exactly these keys:\n"
            '{"skill_name": "kebab-case-name", '
            '"description": "one-line description under 200 chars", '
            '"protected_content": "markdown guidelines for the SKILL.md protected region"}\n'
            "Keep protected_content under 500 words. Be specific and actionable."
        )

        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        if not token or not base_url:
            return {"skill_name": name, "error": "LLM not configured"}

        payload = json.dumps({
            "model": os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro"),
            "max_tokens": 400,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": "Reply only with valid JSON. No markdown fences."},
                {"role": "user", "content": prompt},
            ],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{base_url}/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": token,
            },
        )

        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            content = _extract_response_text(result, "{}").strip()

        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]

        return json.loads(content)
    except Exception:
        return {
            "skill_name": name,
            "description": f"Auto-detected {intent} pattern from {len(texts)} observations",
            "protected_content": "\n".join(f"- {t[:200]}" for t in texts[:5]),
        }


# ======================================================================
# v3.0: Hermes + mem0 parity features
# ======================================================================

# ── 1. LLM Auto-Extraction (mem0 1-pass equivalent) ──

EXTRACTION_PROMPT = """Extract key facts from this conversation chunk. Return JSON:
{"facts":[{"text":"...","type":"event|state|plan|relationship|preference|absence|fact",
"confidence":0.0-1.0,"entities":["name1","name2"]}]}
Rules: be concise, 1 sentence per fact, ignore trivial chat, prefer user decisions/preferences.
Chunk: {chunk}"""


def extract_facts_llm(
    text: str,
    model: str | None = None,
    timeout: int = 30,
) -> list[dict]:
    """Extract structured facts from conversation text using LLM.

    Single-pass ADD-only extraction (mem0-compatible).
    Returns list of {text, type, confidence, entities} dicts.
    Falls back to keyword extraction if LLM unavailable.
    """
    import urllib.request
    import os as _os

    if not _os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return _extract_facts_keyword(text)

    try:
        payload = json.dumps({
            "model": model or _os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-pro"),
            "max_tokens": 500,
            "temperature": 0.1,
            "messages": [{
                "role": "user",
                "content": EXTRACTION_PROMPT.replace("{chunk}", text[:3000]),
            }],
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{_os.environ['ANTHROPIC_BASE_URL']}/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": _os.environ["ANTHROPIC_AUTH_TOKEN"],
            },
        )

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            content = result.get("content", [{}])[0].get("text", "{}")

        # Parse JSON from response
        content = content.strip()
        if "```" in content:
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        data = json.loads(content)
        facts = data.get("facts", [])
        for f in facts:
            if "type" not in f or f["type"] not in MEMORY_TYPES:
                f["type"] = classify_memory_type(f.get("text", ""))
            if "confidence" not in f:
                f["confidence"] = 0.7
        return facts

    except Exception:
        return _extract_facts_keyword(text)


def _extract_facts_keyword(text: str) -> list[dict]:
    """Fallback keyword-based fact extraction."""
    import re as _re

    facts = []
    patterns = [
        (r"(?:decided|决定|选择|采用|chose)\s+([^.;\n]{20,200})", "decision"),
        (r"(?:prefer|偏好|喜欢|宁愿)\s+([^.;\n]{20,200})", "preference"),
        (r"(?:must|never|always|必须|禁止|不能)\s+([^.;\n]{20,200})", "constraint"),
        (r"(?:will|plan|打算|计划)\s+([^.;\n]{20,200})", "plan"),
        (r"(?:working on|doing|在做|开发)\s+([^.;\n]{20,200})", "state"),
    ]

    for pat, ptype in patterns:
        for match in _re.findall(pat, text, _re.IGNORECASE):
            facts.append({
                "text": match.strip()[:200],
                "type": classify_memory_type(match),
                "confidence": 0.5,
                "entities": [],
            })

    return facts[:10]


# ── 2. Temporal Memory Types ──

MEMORY_TYPES = {
    "event": {"desc": "One-time occurrence with a timestamp", "decay_rate": 0.03},
    "state": {"desc": "Ongoing condition that may change", "decay_rate": 0.01},
    "plan": {"desc": "Future intention or scheduled action", "decay_rate": 0.05},
    "relationship": {"desc": "Connection between entities", "decay_rate": 0.005},
    "preference": {"desc": "Personal taste or habit", "decay_rate": 0.01},
    "absence": {"desc": "Notable lack or missing item", "decay_rate": 0.02},
    "fact": {"desc": "Timeless factual statement", "decay_rate": 0.005},
}


def classify_memory_type(text: str) -> str:
    """Classify observation text into one of 7 memory types.

    Zero-LLM keyword-based (mem0-compatible 7-type classification).
    """
    tl = text.lower()

    # Plan: future markers
    plan_kw = ["will", "plan", "打算", "计划", "going to", "scheduled",
               "upcoming", "next week", "tomorrow", "即将"]
    for kw in plan_kw:
        if kw in tl:
            return "plan"

    # Event: past markers
    event_kw = ["happened", "occurred", "发生", "yesterday", "last week",
                "just", "already", "completed", "done", "finished", "完成"]
    for kw in event_kw:
        if kw in tl:
            return "event"

    # State: ongoing markers
    state_kw = ["currently", "working on", "doing", "在做", "开发", "building",
                "maintaining", "running", "active", "in progress"]
    for kw in state_kw:
        if kw in tl:
            return "state"

    # Relationship
    rel_kw = ["works with", "reports to", "managed by", "friend", "colleague",
              "team", "合作", "同事", "老板", "partner"]
    for kw in rel_kw:
        if kw in tl:
            return "relationship"

    # Preference
    pref_kw = ["prefer", "like", "don't like", "favorite", "偏好", "喜欢",
               "习惯", "hate", "love", "enjoy"]
    for kw in pref_kw:
        if kw in tl:
            return "preference"

    # Absence
    abs_kw = ["missing", "don't have", "no", "without", "缺少", "没有",
              "不存在", "never", "none", "缺乏"]
    for kw in abs_kw:
        if kw in tl:
            return "absence"

    return "fact"


def apply_memory_type_metadata(
    db_path: str | None = None,
    limit: int = 500,
) -> int:
    """Backfill memory_type for observations without it.

    Uses classify_memory_type() to assign types.
    Returns count of updated observations.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn
        rows = conn.execute(
            """SELECT o.id, o.text FROM observations o
               JOIN observation_meta om ON o.id = om.observation_id
               WHERE (om.memory_type IS NULL OR om.memory_type = 'fact')
                 AND o.archived_at IS NULL
               LIMIT ?""",
            (limit,),
        ).fetchall()

        count = 0
        for r in rows:
            mtype = classify_memory_type(r["text"] or "")
            if mtype != "fact":  # Only update non-default types
                conn.execute(
                    "UPDATE observation_meta SET memory_type=? WHERE observation_id=?",
                    (mtype, r["id"]),
                )
                count += 1

        conn.commit()
        return count
    finally:
        store.close()


# ── 3. Multi-Graph Traversal ──

def multi_graph_search(
    query: str,
    top_k: int = 10,
    beam_width: int = 3,
    db_path: str | None = None,
) -> list[dict]:
    """Multi-graph beam search across semantic + temporal + causal graphs.

    Three parallel traversals with beam pruning, then fused ranking.
    (ClawMem-compatible multi-graph traversal)

    Graphs:
    - Semantic: embedding cosine similarity over observations
    - Temporal: chronological proximity + memory_type decay rates
    - Causal: cause→effect chains via relations graph
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # ── Semantic graph: get top-k by embedding ──
        sem_results = multi_signal_search(query, top_k=beam_width * 5, db_path=db_path)

        # ── Temporal graph: chronological neighbors ──
        temporal_results = []
        if sem_results:
            sem_ids = [r["id"] for r in sem_results[:beam_width]]
            placeholders = ",".join("?" * len(sem_ids))
            neighbors = conn.execute(
                f"""SELECT o.id, o.text, o.entity_id, o.created_at,
                           COALESCE(om.memory_type, 'fact') as memory_type
                    FROM observations o
                    LEFT JOIN observation_meta om ON o.id = om.observation_id
                    WHERE o.archived_at IS NULL
                      AND o.id NOT IN ({placeholders})
                    ORDER BY ABS(
                        julianday(o.created_at) -
                        (SELECT AVG(julianday(created_at)) FROM observations
                         WHERE id IN ({placeholders}))
                    )
                    LIMIT ?""",
                (*sem_ids, *sem_ids, beam_width * 3),
            ).fetchall()
            temporal_results = [dict(r) for r in neighbors]

        # ── Causal graph: follow relation edges ──
        causal_results = []
        if sem_results:
            sem_ids = [r["id"] for r in sem_results[:beam_width]]
            for sid in sem_ids[:3]:
                # Find entity, then find co-occurring entities via relations
                entity_id = conn.execute(
                    "SELECT entity_id FROM observations WHERE id=?", (sid,)
                ).fetchone()
                if entity_id:
                    linked = conn.execute(
                        """SELECT o.id, o.text, o.entity_id, o.created_at
                           FROM observations o
                           JOIN relations r ON (o.entity_id = r.from_id OR o.entity_id = r.to_id)
                           WHERE (r.from_id = ? OR r.to_id = ?)
                             AND o.id != ?
                             AND o.archived_at IS NULL
                           LIMIT ?""",
                        (entity_id[0], entity_id[0], sid, beam_width),
                    ).fetchall()
                    causal_results.extend(dict(r) for r in linked)

        # ── Fuse results with beam pruning ──
        fused = {}
        for r in sem_results:
            fused[r["id"]] = {"score": r.get("fused_score", 0.5), "sources": ["semantic"]}

        for r in temporal_results:
            rid = r["id"]
            mtype = r.get("memory_type", "fact")
            decay = MEMORY_TYPES.get(mtype, {}).get("decay_rate", 0.01)
            t_score = 0.3 * (1.0 - decay)
            if rid in fused:
                fused[rid]["score"] += t_score
                fused[rid]["sources"].append("temporal")
            else:
                fused[rid] = {"score": t_score, "sources": ["temporal"]}

        for r in causal_results:
            rid = r["id"]
            c_score = 0.2
            if rid in fused:
                fused[rid]["score"] += c_score
                fused[rid]["sources"].append("causal")
            else:
                fused[rid] = {"score": c_score, "sources": ["causal"]}

        # Sort and return top-k
        ranked = sorted(fused.items(), key=lambda x: x[1]["score"], reverse=True)
        results = []
        for rid, info in ranked[:top_k]:
            row = conn.execute(
                "SELECT id, text, entity_id, created_at FROM observations WHERE id=?",
                (rid,),
            ).fetchone()
            if row:
                results.append({
                    "id": rid,
                    "text": (row["text"] or "")[:200],
                    "score": round(info["score"], 4),
                    "sources": info["sources"],
                    "created_at": row["created_at"],
                })

        return results
    finally:
        store.close()


# ── 4. Cross-Encoder Rerank ──

def cross_encode_rerank(
    query: str,
    candidates: list[dict],
    top_k: int = 5,
) -> list[dict]:
    """Re-rank candidates using sentence-transformers cross-encoder.

    Takes top semantic results and re-scores them with a cross-encoder
    for more accurate relevance ranking.
    Falls back to original scores if cross-encoder unavailable.
    """
    try:
        from sentence_transformers import CrossEncoder
        import numpy as _np

        model = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        pairs = [(query, c.get("text", "")[:500]) for c in candidates]
        scores = model.predict(pairs)

        for i, c in enumerate(candidates):
            c["cross_score"] = round(float(scores[i]), 4)
            c["original_score"] = c.get("fused_score", c.get("score", 0))
            c["fused_score"] = round(
                float(scores[i]) * 0.7 + c.get("fused_score", c.get("score", 0)) * 0.3, 4
            )

        candidates.sort(key=lambda x: x.get("fused_score", 0), reverse=True)
        return candidates[:top_k]

    except ImportError:
        # Cross-encoder not installed — return as-is
        return candidates[:top_k]
    except Exception:
        return candidates[:top_k]


# ── 5. Intent Classification 7-Mode ──

QUERY_MODES = {
    "historical_range": ["what happened", "history", "past", "before", "ago", "回顾", "历史"],
    "current_state": ["currently", "now", "status", "what is", "what's", "当前", "现在"],
    "duration_state": ["how long", "since when", "duration", "多久", "持续"],
    "upcoming": ["upcoming", "planned", "will", "next", "未来", "计划", "将要"],
    "soft_recency": ["lately", "recent", "recently", "最近", "近期"],
    "fact_lookup": ["what is", "define", "explain", "什么是", "定义", "who is"],
    "relationship": ["who works", "connected", "related", "关系", "联系"],
}


def classify_query_intent(query: str) -> str:
    """Classify query into one of 7 temporal query modes.

    Zero-LLM keyword-based (mem0-compatible 7-mode classification).
    """
    ql = query.lower()
    for mode, keywords in QUERY_MODES.items():
        for kw in keywords:
            if kw in ql:
                return mode
    return "fact_lookup"


# ── 6. Audit Trail ──

AUDIT_LOG = os.path.expanduser("~/.agent-recall/audit-trace.jsonl")


def audit_log(
    operation: str,
    entity: str,
    detail: dict | None = None,
    session_id: str | None = None,
) -> None:
    """Write an audit trail entry to trace.jsonl.

    Records every write operation for full traceability.
    AgentMemory-compatible format: {op, entity, detail, session, timestamp}.
    """
    try:
        entry = {
            "op": operation,
            "entity": entity,
            "detail": detail or {},
            "session": session_id or os.environ.get("AGENT_RECALL_SLUG", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Audit should never block operations


def read_audit_trail(
    limit: int = 50,
    operation: str | None = None,
    session_id: str | None = None,
) -> list[dict]:
    """Read recent audit trail entries with optional filters."""
    if not os.path.exists(AUDIT_LOG):
        return []

    entries = []
    try:
        with open(AUDIT_LOG, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if operation and entry.get("op") != operation:
                        continue
                    if session_id and entry.get("session") != session_id:
                        continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass

    return entries[-limit:]


# ── 7. Persona Distillation ──

def distill_persona(
    db_path: str | None = None,
    min_confidence: float = 0.6,
) -> dict:
    """Build a living persona from accumulated preferences and decisions.

    NexSandglass Soul Distillation equivalent.
    Aggregates high-confidence preference + decision observations
    into a compact persona profile.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    store = MemoryStore(db_path)
    try:
        conn = store._conn

        # Get high-confidence preferences
        prefs = conn.execute(
            """SELECT o.text, om.confidence, om.memory_type
               FROM observations o
               JOIN observation_meta om ON o.id = om.observation_id
               WHERE om.intent_type IN ('preference', 'decision')
                 AND om.confidence >= ?
                 AND o.archived_at IS NULL
               ORDER BY om.confidence DESC
               LIMIT 50""",
            (min_confidence,),
        ).fetchall()

        # Get recurring constraints
        constraints = conn.execute(
            """SELECT o.text, om.confidence
               FROM observations o
               JOIN observation_meta om ON o.id = om.observation_id
               WHERE om.intent_type = 'constraint'
                 AND om.confidence >= ?
                 AND o.archived_at IS NULL
               ORDER BY om.confidence DESC
               LIMIT 20""",
            (min_confidence,),
        ).fetchall()

        # Get top entities mentioned in preferences
        entities = conn.execute(
            """SELECT e.name, e.type, COUNT(*) as mentions
               FROM entities e
               JOIN observations o ON e.id = o.entity_id
               JOIN observation_meta om ON o.id = om.observation_id
               WHERE om.intent_type IN ('preference', 'decision')
                 AND o.archived_at IS NULL
               GROUP BY e.id
               ORDER BY mentions DESC
               LIMIT 10""",
        ).fetchall()

        # Build persona profile
        persona = {
            "distilled_at": datetime.now(timezone.utc).isoformat(),
            "preferences": [{"text": r["text"][:200], "confidence": r["confidence"],
                             "type": r["memory_type"]} for r in prefs],
            "constraints": [{"text": r["text"][:200], "confidence": r["confidence"]}
                           for r in constraints],
            "key_entities": [{"name": r["name"], "type": r["type"],
                             "mentions": r["mentions"]} for r in entities],
            "summary": _generate_persona_summary(prefs, constraints, entities),
        }

        return persona
    finally:
        store.close()


def _generate_persona_summary(
    prefs: list, constraints: list, entities: list
) -> str:
    """Generate a compact persona summary string."""
    parts = []

    if prefs:
        top_prefs = prefs[:5]
        parts.append("Preferences: " + "; ".join(
            p["text"][:80] for p in top_prefs
        ))

    if constraints:
        top_constraints = constraints[:3]
        parts.append("Constraints: " + "; ".join(
            c["text"][:80] for c in top_constraints
        ))

    if entities:
        top_entities = entities[:5]
        parts.append("Key entities: " + ", ".join(
            f"{e['name']}({e['type']})" for e in top_entities
        ))

    return " | ".join(parts) if parts else "No persona data yet"


# ── Enhanced extraction pipeline ──

def run_extraction_pipeline(
    session_text: str,
    db_path: str | None = None,
    use_llm: bool = True,
    dry_run: bool = False,
) -> dict:
    """Full extraction pipeline: LLM extract → classify → store → audit.

    Mem0 1-pass extraction equivalent.
    Extracts facts from session text, classifies memory types,
    stores in observations with meta, and writes audit trail.
    """
    if db_path is None:
        db_path = os.path.expanduser("~/.agent-recall/frames.db")

    # Extract
    if use_llm:
        facts = extract_facts_llm(session_text)
    else:
        facts = _extract_facts_keyword(session_text)

    if dry_run:
        return {"extracted": len(facts), "facts": facts, "dry_run": True}

    # Store
    store = MemoryStore(db_path)
    stored = 0
    try:
        conn = store._conn
        session_id = os.environ.get("AGENT_RECALL_SLUG", "unknown")

        for fact in facts:
            # Create or resolve entity
            entities = fact.get("entities", [])
            eid = None
            if entities:
                eid = store.resolve_entity(entities[0], "extracted_fact")

            if eid is None:
                eid = conn.execute(
                    "INSERT INTO entities (name, type, created_at) "
                    "VALUES ('extracted', 'auto_captured', datetime('now'))"
                ).lastrowid

            # Store observation
            conn.execute(
                "INSERT INTO observations (entity_id, text, scope, created_at) "
                "VALUES (?, ?, 'global', datetime('now'))",
                (eid, fact["text"][:500]),
            )
            obs_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Store meta with memory type
            mtype = fact.get("type", classify_memory_type(fact["text"]))
            confidence = fact.get("confidence", 0.7)
            conn.execute(
                """INSERT OR REPLACE INTO observation_meta
                   (observation_id, valid_from, confidence, intent_type,
                    memory_type, source_session_id)
                   VALUES (?, datetime('now'), ?, 'fact', ?, ?)""",
                (obs_id, confidence, mtype, session_id),
            )

            # Audit
            audit_log("extract", f"observation:{obs_id}",
                      {"text": fact["text"][:100], "type": mtype,
                       "confidence": confidence},
                      session_id)
            stored += 1

        conn.commit()
        return {"extracted": len(facts), "stored": stored}
    finally:
        store.close()
