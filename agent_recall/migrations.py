"""Schema migration system for agent-recall databases.

Simple, file-less migration system. Each migration is a Python function
registered in MIGRATIONS. Migrations run in order, each updating the
schema version. New databases start at the latest version.

Usage::

    from agent_recall.migrations import run_migrations
    run_migrations(conn)  # called automatically by MemoryStore.__init__
"""

from __future__ import annotations

import sqlite3
from typing import Callable

# Type alias for migration functions
MigrationFn = Callable[[sqlite3.Connection], None]


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    """Create the schema_version table if it doesn't exist."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(version INTEGER PRIMARY KEY)"
    )


def get_version(conn: sqlite3.Connection) -> int | None:
    """Get the current schema version, or None if no version is set.

    Returns None for databases that predate the migration system.
    """
    _ensure_version_table(conn)
    row = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return row[0]


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    """Set the schema version (inside an existing transaction)."""
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


# ---------------------------------------------------------------------------
# Migration functions
# ---------------------------------------------------------------------------
# Each migration receives an open sqlite3.Connection and is run inside a
# transaction. Do NOT commit or begin transactions inside migrations.
#
# Convention: migrate_NNN_short_description(conn)
# ---------------------------------------------------------------------------

def migrate_001_baseline(conn: sqlite3.Connection) -> None:
    """Baseline migration for existing databases.

    All tables already use IF NOT EXISTS, so existing databases are fine.
    This migration just establishes the version tracking baseline.
    """
    # Nothing to do -- the version bump is handled by run_migrations.
    pass


def migrate_002_knowledge_tiers(conn: sqlite3.Connection) -> None:
    """Add knowledge_tiers table for 3-tier memory (hot/warm/cold)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_tiers (
            observation_id INTEGER PRIMARY KEY,
            tier TEXT NOT NULL DEFAULT 'warm'
                CHECK(tier IN ('hot', 'warm', 'cold')),
            salience_score REAL NOT NULL DEFAULT 0.5,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed_at TEXT,
            promoted_at TEXT,
            promotion_source TEXT DEFAULT 'auto',
            decay_factor REAL NOT NULL DEFAULT 0.01,
            base_importance REAL NOT NULL DEFAULT 0.5,
            FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_tiers_tier ON knowledge_tiers(tier);
        CREATE INDEX IF NOT EXISTS idx_tiers_salience
            ON knowledge_tiers(salience_score DESC);
    """)


def migrate_003_embeddings(conn: sqlite3.Connection) -> None:
    """Add observation_embeddings virtual table (requires sqlite-vec).

    Uses sqlite_vec Python binding to load the extension properly
    (Windows-safe, enables extension loading before load).
    Gracefully skipped if sqlite-vec is not installed.
    """
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS observation_embeddings USING vec0(
                embedding float[768],
                observation_id INTEGER,
                entity_id INTEGER
            );
        """)
    except ImportError:
        pass
    except Exception:
        pass


def migrate_004_token_budgets(conn: sqlite3.Connection) -> None:
    """Add token_budgets table for per-scope token management."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS token_budgets (
            scope TEXT PRIMARY KEY,
            budget_tokens INTEGER NOT NULL DEFAULT 4000,
            used_tokens INTEGER NOT NULL DEFAULT 0,
            last_reset TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)


def migrate_005_pattern_store(conn: sqlite3.Connection) -> None:
    """Add pattern_store table for auto-captured patterns."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pattern_store (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_hash TEXT UNIQUE NOT NULL,
            pattern_text TEXT NOT NULL,
            pattern_type TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_entity_type TEXT,
            metadata TEXT DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_pattern_type ON pattern_store(pattern_type);
    """)


def migrate_006_retrieval_trust(conn: sqlite3.Connection) -> None:
    """Add retrieval_events and trust_events tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS retrieval_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_hash TEXT NOT NULL,
            observation_id INTEGER NOT NULL,
            similarity REAL,
            was_used INTEGER NOT NULL DEFAULT 0,
            feedback TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_ret_query
            ON retrieval_events(query_hash, created_at);

        CREATE TABLE IF NOT EXISTS trust_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observation_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            old_trust REAL NOT NULL,
            new_trust REAL NOT NULL,
            delta REAL NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
        );
    """)


def migrate_007_privacy_patterns(conn: sqlite3.Connection) -> None:
    """Add observation_privacy and access_patterns tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observation_privacy (
            observation_id INTEGER PRIMARY KEY,
            privacy_level TEXT NOT NULL DEFAULT 'public'
                CHECK(privacy_level IN ('public', 'private', 'sensitive', 'redacted')),
            tagged_by TEXT DEFAULT 'agent',
            FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS access_patterns (
            from_observation_id INTEGER NOT NULL,
            to_observation_id INTEGER NOT NULL,
            transition_count INTEGER NOT NULL DEFAULT 1,
            probability REAL NOT NULL DEFAULT 0.0,
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (from_observation_id, to_observation_id),
            FOREIGN KEY (from_observation_id) REFERENCES observations(id) ON DELETE CASCADE,
            FOREIGN KEY (to_observation_id) REFERENCES observations(id) ON DELETE CASCADE
        );
    """)


def migrate_008_new_indexes(conn: sqlite3.Connection) -> None:
    """Add performance indexes on observations table."""
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_obs_created_at ON observations(created_at);
        CREATE INDEX IF NOT EXISTS idx_obs_entity_scope
            ON observations(entity_id, scope) WHERE archived_at IS NULL;
    """)


# ---------------------------------------------------------------------------
# OMC (Online Memory & Cognition) migrations — P0
# ---------------------------------------------------------------------------


def migrate_009_observation_meta(conn: sqlite3.Connection) -> None:
    """Add temporal validity, confidence, intent classification to observations."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observation_meta (
            observation_id INTEGER PRIMARY KEY,
            valid_from TEXT,
            valid_to TEXT,
            confidence REAL DEFAULT 0.5,
            intent_type TEXT,
            source_session_id TEXT,
            FOREIGN KEY (observation_id) REFERENCES observations(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_meta_intent ON observation_meta(intent_type);
        CREATE INDEX IF NOT EXISTS idx_meta_confidence ON observation_meta(confidence);
        CREATE INDEX IF NOT EXISTS idx_meta_valid_to ON observation_meta(valid_to);
    """)


def migrate_010_edits(conn: sqlite3.Connection) -> None:
    """Add SkillOpt Edit type system tables."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS edits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            op TEXT NOT NULL CHECK(op IN ('append','insert_after','replace','delete')),
            content TEXT NOT NULL,
            target TEXT NOT NULL,
            support_count INTEGER DEFAULT 1,
            source_type TEXT NOT NULL CHECK(source_type IN ('failure','success','correction','preference','pattern','bugfix')),
            merge_level INTEGER DEFAULT 0,
            update_origin TEXT NOT NULL,
            update_target TEXT NOT NULL,
            patch_id INTEGER,
            status TEXT DEFAULT 'candidate' CHECK(status IN ('candidate','selected','applied','validated','rejected','rolled_back')),
            created_at TEXT NOT NULL,
            applied_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_edits_status ON edits(status);
        CREATE INDEX IF NOT EXISTS idx_edits_target ON edits(update_target);

        CREATE TABLE IF NOT EXISTS patches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epoch_id INTEGER,
            reasoning TEXT,
            ranking_details TEXT DEFAULT '{}',
            update_mode TEXT NOT NULL CHECK(update_mode IN ('patch','rewrite_from_suggestions','full_rewrite_minibatch')),
            edit_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS rollout_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            hard_score INTEGER,
            soft_score REAL,
            n_turns INTEGER,
            fail_reason TEXT,
            task_description TEXT,
            predicted_answer TEXT,
            reference_text TEXT,
            skill_snapshot_hash TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_rollout_session ON rollout_results(session_id);
    """)


def migrate_011_epoch_boundaries(conn: sqlite3.Connection) -> None:
    """Add epoch boundary tracking for ReflACT pipeline."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS epoch_boundaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epoch_number INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            edits_total INTEGER DEFAULT 0,
            edits_accepted INTEGER DEFAULT 0,
            edits_rejected INTEGER DEFAULT 0,
            heldout_score_before REAL,
            heldout_score_after REAL
        );
        CREATE INDEX IF NOT EXISTS idx_epoch_number ON epoch_boundaries(epoch_number);
    """)


# ---------------------------------------------------------------------------
# OMC migrations — P1 (schema created early, logic comes later)
# ---------------------------------------------------------------------------


def migrate_012_learning_rate_state(conn: sqlite3.Connection) -> None:
    """Add learning rate scheduler state for evolution pipeline."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS learning_rate_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduler_type TEXT NOT NULL DEFAULT 'autonomous',
            initial_lr REAL NOT NULL DEFAULT 0.3,
            current_lr REAL NOT NULL DEFAULT 0.3,
            min_lr REAL DEFAULT 0.05,
            max_lr REAL DEFAULT 0.5,
            warmup_steps INTEGER DEFAULT 3,
            total_steps INTEGER DEFAULT 50,
            current_step INTEGER DEFAULT 0,
            updated_at TEXT NOT NULL
        );
    """)


def migrate_013_protected_regions(conn: sqlite3.Connection) -> None:
    """Add document protected regions for edit safety."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS protected_regions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_path TEXT NOT NULL,
            region_type TEXT NOT NULL CHECK(region_type IN ('slow_update','appendix','identity','constraints')),
            start_marker TEXT NOT NULL,
            end_marker TEXT NOT NULL,
            description TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_protected_skill ON protected_regions(skill_path);
    """)


def migrate_014_meta_strategies(conn: sqlite3.Connection) -> None:
    """Add meta-optimizer strategy tracking."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta_strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_name TEXT NOT NULL UNIQUE,
            target_field TEXT NOT NULL,
            success_rate REAL DEFAULT 0.0,
            applied_count INTEGER DEFAULT 0,
            last_applied TEXT,
            parameters TEXT DEFAULT '{}'
        );
    """)


def migrate_015_skill_route_tables(conn: sqlite3.Connection) -> None:
    """P3: Add skill route index tables for real-time prompt-to-skill matching."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS skill_route_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL UNIQUE,
            raw_name TEXT,
            description TEXT,
            file_path TEXT,
            file_mtime REAL,
            triggers_json TEXT DEFAULT '[]',
            tags_json TEXT DEFAULT '[]',
            category TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            first_seen TEXT NOT NULL DEFAULT (datetime('now')),
            last_seen TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_route_skill_name ON skill_route_index(skill_name);
        CREATE INDEX IF NOT EXISTS idx_route_active ON skill_route_index(is_active);

        CREATE VIRTUAL TABLE IF NOT EXISTS skill_route_fts USING fts5(
            skill_name, description, triggers_text, tags_text,
            tokenize='porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS skill_route_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            pattern TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            sample_queries TEXT DEFAULT '[]',
            hit_count INTEGER DEFAULT 0,
            last_hit TEXT,
            FOREIGN KEY (skill_name) REFERENCES skill_route_index(skill_name),
            UNIQUE(skill_name, pattern)
        );
        CREATE INDEX IF NOT EXISTS idx_route_pat_skill ON skill_route_patterns(skill_name);

        CREATE TABLE IF NOT EXISTS skill_route_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            query_hash TEXT NOT NULL,
            query_snippet TEXT,
            score REAL,
            source TEXT,
            was_used INTEGER DEFAULT 0,
            session_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_route_hits_skill ON skill_route_hits(skill_name);
        CREATE INDEX IF NOT EXISTS idx_route_hits_created ON skill_route_hits(created_at);
    """)
    # Add columns to skill_performance (idempotent)
    for col, col_type in [
        ("route_hit_count", "INTEGER DEFAULT 0"),
        ("route_accept_rate", "REAL DEFAULT 0.0"),
        ("route_last_matched", "TEXT"),
        ("route_keywords", "TEXT"),
        ("router_enabled", "INTEGER DEFAULT 1"),
    ]:
        try:
            conn.execute(f"ALTER TABLE skill_performance ADD COLUMN {col} {col_type}")
        except Exception:
            pass


def migrate_016_skill_embeddings(conn: sqlite3.Connection) -> None:
    """Add 384-dim skill embeddings vec0 table for semantic matching.

    Uses all-MiniLM-L6-v2 via sentence-transformers.
    Gracefully skipped if sqlite-vec not installed.
    """
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS skill_embeddings USING vec0(
                embedding float[384],
                skill_name TEXT,
                description TEXT
            );
        """)
    except (ImportError, Exception):
        pass


def migrate_017_omc_schema_fixes(conn: sqlite3.Connection) -> None:
    """Fix schema gaps: rejected_edit_buffer, valid_time columns, missing columns.

    - B1: rejected_edit_buffer table (was never created)
    - B2: edits.skill_name column (gate_edit queries it but it didn't exist)
    - P9: observations.valid_from / valid_to (bitemporal support)
    - memory_type on observation_meta (referenced but missing)
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rejected_edit_buffer (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT NOT NULL,
            op TEXT NOT NULL,
            content TEXT NOT NULL,
            reason_rejected TEXT,
            attempt_count INTEGER DEFAULT 1,
            epoch_number INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_rejected_skill ON rejected_edit_buffer(skill_name);
    """)
    # ALTER TABLE with try/except — columns may already exist
    for col_sql in [
        "ALTER TABLE edits ADD COLUMN skill_name TEXT",
        "ALTER TABLE observations ADD COLUMN valid_from TEXT",
        "ALTER TABLE observations ADD COLUMN valid_to TEXT",
        "ALTER TABLE observation_meta ADD COLUMN memory_type TEXT DEFAULT 'fact'",
    ]:
        try:
            conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # Column already exists
    # Backfill valid_from = created_at for existing rows
    conn.execute(
        "UPDATE observations SET valid_from = created_at WHERE valid_from IS NULL"
    )


def migrate_018_observation_embeddings(conn: sqlite3.Connection) -> None:
    """Rebuild observation_embeddings vec0 with 384d + backfill active observations.

    Original migrate_003 created a 768d vec0 that was never populated.
    This rebuilds it with the correct 384d (all-MiniLM-L6-v2) and backfills.
    """
    import struct
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except (ImportError, Exception):
        return

    def _vec_to_blob(vec):
        return struct.pack(f'{len(vec)}f', *vec)

    conn.execute("DROP TABLE IF EXISTS observation_embeddings")
    conn.executescript("""
        CREATE VIRTUAL TABLE observation_embeddings USING vec0(
            embedding float[384],
            observation_id INTEGER,
            entity_id INTEGER
        );
    """)

    # Backfill: embed all active observations
    try:
        from agent_recall.embeddings import get_provider
        provider = get_provider()
        if provider is None:
            return
        rows = conn.execute(
            "SELECT o.id, o.text, o.entity_id FROM observations o "
            "WHERE o.archived_at IS NULL AND o.text IS NOT NULL AND o.text != ''"
        ).fetchall()
        if not rows:
            return
        texts = [r["text"] for r in rows]
        embeddings = provider.embed_batch(texts)
        data = [
            (_vec_to_blob(e), r["id"], r["entity_id"])
            for e, r in zip(embeddings, rows)
        ]
        conn.executemany(
            "INSERT INTO observation_embeddings(embedding, observation_id, entity_id) "
            "VALUES (?, ?, ?)",
            data,
        )
    except Exception:
        pass  # Best-effort backfill


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------
# Ordered list of (version, function) tuples. Version numbers start at 1
# and must be consecutive.
MIGRATIONS: list[tuple[int, MigrationFn]] = [
    (1, migrate_001_baseline),
    (2, migrate_002_knowledge_tiers),
    (3, migrate_003_embeddings),
    (4, migrate_004_token_budgets),
    (5, migrate_005_pattern_store),
    (6, migrate_006_retrieval_trust),
    (7, migrate_007_privacy_patterns),
    (8, migrate_008_new_indexes),
    # OMC P0
    (9, migrate_009_observation_meta),
    (10, migrate_010_edits),
    (11, migrate_011_epoch_boundaries),
    # OMC P1
    (12, migrate_012_learning_rate_state),
    (13, migrate_013_protected_regions),
    (14, migrate_014_meta_strategies),
    # Skill Router
    (15, migrate_015_skill_route_tables),
    (16, migrate_016_skill_embeddings),
    # P9-P12 schema fixes
    (17, migrate_017_omc_schema_fixes),
    (18, migrate_018_observation_embeddings),
]

# The latest schema version (for stamping new databases).
LATEST_VERSION: int = MIGRATIONS[-1][0] if MIGRATIONS else 0


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations to the database.

    Called automatically by ``MemoryStore.__init__`` after table creation.

    For brand-new databases (no schema_version table or no version row),
    the version is stamped to ``LATEST_VERSION`` without running migrations,
    since ``_init_tables()`` already creates the current schema.

    For existing databases, migrations are applied in order. Each migration
    runs inside its own transaction for atomicity -- if a migration fails,
    only that migration is rolled back and the error propagates.

    Args:
        conn: An open SQLite connection (with ``journal_mode=WAL`` recommended).

    Returns:
        The schema version after migration.

    Raises:
        Exception: If any migration function fails. The failed migration is
            rolled back; earlier migrations that succeeded are preserved.
    """
    _ensure_version_table(conn)

    current = get_version(conn)

    if current is None:
        # New database or pre-migration database.
        # Check if this is genuinely new (no entities table data) or
        # an existing database that predates migrations.
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='entities'"
        ).fetchone()
        has_entities_table = row[0] > 0

        if has_entities_table:
            # Check if there's any data -- empty entities table means new DB
            data_row = conn.execute(
                "SELECT COUNT(*) FROM entities"
            ).fetchone()
            has_data = data_row[0] > 0
        else:
            has_data = False

        if not has_data:
            # Brand new database -- stamp to latest, no migrations needed
            _set_version(conn, LATEST_VERSION)
            conn.commit()
            return LATEST_VERSION
        else:
            # Existing database without version tracking -- start from 0
            current = 0
            _set_version(conn, 0)
            conn.commit()

    # Apply pending migrations
    for version, migrate_fn in MIGRATIONS:
        if version <= current:
            continue
        # Each migration in its own transaction
        conn.execute("BEGIN IMMEDIATE")
        try:
            migrate_fn(conn)
            _set_version(conn, version)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        current = version

    return current
