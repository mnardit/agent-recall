"""State Layer — single SQLite database for all agent memory.

Single DB with scope column on slots for hierarchical filtering.
Tables: entities, slots (bitemporal+scoped), observations, relations, log_entries, documents.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class _Transaction:
    """Context manager that wraps store operations in a single SQLite transaction."""

    def __init__(self, store: "MemoryStore") -> None:
        self._store = store
        self._conn = store._conn

    def __enter__(self) -> "_Transaction":
        self._conn.execute("BEGIN IMMEDIATE")
        self._store._in_transaction = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._store._in_transaction = False


class MemoryStore:
    """SQLite-backed memory store for AI agents.

    Provides entity management with scoped slots (bitemporal), observations,
    relations, log entries, and full-text search. Supports context manager
    protocol (``with MemoryStore(path) as store: ...``).

    Args:
        db_path: Path to SQLite database file. Created if it doesn't exist.
            Defaults to ``~/.agent-recall/frames.db`` if not provided.
        timeout: SQLite busy timeout in seconds (default 10).

    Example::

        with MemoryStore("memory.db") as store:
            eid = store.resolve_entity("Alice", "person")
            store.set_slot(eid, "role", "Engineer", scope="acme")
            store.add_observation(eid, "Prefers async communication")
    """

    def __init__(self, db_path: Path | str | None = None, timeout: float = 10.0) -> None:
        if db_path is None:
            from agent_recall.config import DEFAULT_DB_PATH
            db_path = DEFAULT_DB_PATH
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._conn = sqlite3.connect(str(self.db_path), timeout=timeout,
                                      check_same_thread=False)
        if self.db_path.exists():
            os.chmod(str(self.db_path), 0o600)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA wal_autocheckpoint=100")  # 防WAL堆积 400KB自检
        self._conn.execute("PRAGMA foreign_keys=ON")
        # ponytail: SQLite perf — cache 64MB + mmap 256MB + mem temp store
        self._conn.execute("PRAGMA cache_size = -64000")
        self._conn.execute("PRAGMA mmap_size = 268435456")
        self._conn.execute("PRAGMA temp_store = MEMORY")
        # Enable extension loading for sqlite-vec (Windows requires this)
        try:
            self._conn.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(self._conn)
        except Exception:
            pass
        self._in_transaction = False
        self._write_count = 0
        self._last_rebalance = datetime.now(timezone.utc)
        self._init_tables()
        self._run_migrations()

    def _run_migrations(self) -> None:
        """Apply any pending schema migrations."""
        from agent_recall.migrations import run_migrations
        run_migrations(self._conn)

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name_type ON entities(name, type);
            CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

            CREATE TABLE IF NOT EXISTS slots (
                entity_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                scope TEXT NOT NULL DEFAULT 'global',
                confidence REAL DEFAULT 1.0,
                valid_from TEXT NOT NULL,
                valid_to TEXT,
                source TEXT DEFAULT 'agent',
                PRIMARY KEY (entity_id, key, scope, valid_from),
                FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_slots_current
                ON slots(entity_id, key, scope) WHERE valid_to IS NULL;

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global',
                created_at TEXT NOT NULL,
                archived_at TEXT,
                FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_obs_entity
                ON observations(entity_id) WHERE archived_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_obs_created_at ON observations(created_at);
            CREATE INDEX IF NOT EXISTS idx_obs_entity_scope
                ON observations(entity_id, scope) WHERE archived_at IS NULL;

            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'global',
                status TEXT DEFAULT 'active',
                context TEXT,
                created_at TEXT NOT NULL,
                archived_at TEXT,
                FOREIGN KEY (from_id) REFERENCES entities(id) ON DELETE CASCADE,
                FOREIGN KEY (to_id) REFERENCES entities(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_rels_from
                ON relations(from_id) WHERE status = 'active';
            CREATE INDEX IF NOT EXISTS idx_rels_to
                ON relations(to_id) WHERE status = 'active';

            CREATE TABLE IF NOT EXISTS log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                text TEXT NOT NULL,
                author TEXT DEFAULT 'agent',
                created_at TEXT NOT NULL,
                FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_logs_entity
                ON log_entries(entity_id, date);

            CREATE TABLE IF NOT EXISTS documents (
                name TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- ═══════════════════════════════════════
            -- v0.5.0: Knowledge lifecycle tables
            -- ═══════════════════════════════════════

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

            CREATE TABLE IF NOT EXISTS token_budgets (
                scope TEXT PRIMARY KEY,
                budget_tokens INTEGER NOT NULL DEFAULT 4000,
                used_tokens INTEGER NOT NULL DEFAULT 0,
                last_reset TEXT NOT NULL DEFAULT (datetime('now'))
            );

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
        self._conn.commit()
        self._init_fts()

    def _init_fts(self) -> None:
        """Create FTS5 virtual tables and sync triggers if FTS5 is available."""
        try:
            self._conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(
                    name,
                    content=entities,
                    content_rowid=id,
                    tokenize='porter unicode61'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
                    text,
                    content=observations,
                    content_rowid=id,
                    tokenize='porter unicode61'
                );

                -- Triggers to keep entities_fts in sync
                CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
                    INSERT INTO entities_fts(rowid, name) VALUES (new.id, new.name);
                END;
                CREATE TRIGGER IF NOT EXISTS entities_ad AFTER DELETE ON entities BEGIN
                    INSERT INTO entities_fts(entities_fts, rowid, name)
                        VALUES('delete', old.id, old.name);
                END;
                CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE ON entities BEGIN
                    INSERT INTO entities_fts(entities_fts, rowid, name)
                        VALUES('delete', old.id, old.name);
                    INSERT INTO entities_fts(rowid, name) VALUES (new.id, new.name);
                END;

                -- Triggers to keep observations_fts in sync
                CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations BEGIN
                    INSERT INTO observations_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations BEGIN
                    INSERT INTO observations_fts(observations_fts, rowid, text)
                        VALUES('delete', old.id, old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations BEGIN
                    INSERT INTO observations_fts(observations_fts, rowid, text)
                        VALUES('delete', old.id, old.text);
                    INSERT INTO observations_fts(rowid, text) VALUES (new.id, new.text);
                END;
            """)
            self._conn.commit()
            self._has_fts = True
        except Exception:
            # FTS5 not available (old SQLite build) — fall back to LIKE
            self._has_fts = False

    def rebuild_fts(self) -> None:
        """Repopulate FTS indexes from existing data.

        Call this after upgrading an existing database to add FTS support,
        or if the FTS index becomes out of sync.

        Raises:
            RuntimeError: If FTS5 is not available.
        """
        if not self._has_fts:
            raise RuntimeError("FTS5 is not available in this SQLite build")
        # Use the FTS5 'rebuild' command to reconstruct from content tables
        self._conn.execute(
            "INSERT INTO entities_fts(entities_fts) VALUES('rebuild')"
        )
        self._conn.execute(
            "INSERT INTO observations_fts(observations_fts) VALUES('rebuild')"
        )
        self._conn.commit()

    @property
    def has_fts(self) -> bool:
        """Whether FTS5 full-text search is available."""
        return self._has_fts

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Entities ---

    def create_entity(self, name: str, entity_type: str) -> int:
        """Create a new entity. Raises ValueError if name/type is empty.

        Returns:
            The integer ID of the created entity.

        Raises:
            sqlite3.IntegrityError: If an entity with this name+type already exists.
        """
        if not name or not name.strip():
            raise ValueError("Entity name cannot be empty")
        if not entity_type or not entity_type.strip():
            raise ValueError("Entity type cannot be empty")
        with self._auto_commit():
            cur = self._conn.execute(
                "INSERT INTO entities (name, type, created_at) VALUES (?, ?, ?)",
                (name, entity_type, self._now()),
            )
        return cur.lastrowid

    def find_entity(self, name: str, entity_type: str | None = None) -> int | None:
        """Find an entity by name and optional type. Returns ID or None."""
        if entity_type:
            row = self._conn.execute(
                "SELECT id FROM entities WHERE name = ? AND type = ?",
                (name, entity_type),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id FROM entities WHERE name = ?", (name,),
            ).fetchone()
        return row["id"] if row else None

    def find_entity_icase(self, name: str, entity_type: str | None = None) -> int | None:
        """Case-insensitive entity lookup. Returns entity ID or None."""
        if entity_type:
            row = self._conn.execute(
                "SELECT id FROM entities WHERE LOWER(name) = LOWER(?) AND type = ?",
                (name, entity_type),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT id FROM entities WHERE LOWER(name) = LOWER(?)", (name,),
            ).fetchone()
        return row["id"] if row else None

    def resolve_entity(self, name: str, entity_type: str) -> int:
        """Find or create an entity. Returns the entity ID.

        Unlike ``create_entity``, this is idempotent — safe to call repeatedly.
        """
        if not name or not name.strip():
            raise ValueError("Entity name cannot be empty")
        with self._auto_commit():
            self._conn.execute(
                "INSERT OR IGNORE INTO entities (name, type, created_at) VALUES (?, ?, ?)",
                (name, entity_type, self._now()),
            )
        return self.find_entity(name, entity_type)

    def get_entity(self, entity_id: int) -> dict | None:
        """Get entity by ID. Returns dict with id, name, type, created_at or None."""
        row = self._conn.execute(
            "SELECT id, name, type, created_at FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_entities(self, entity_type: str | None = None) -> list[dict]:
        """List all entities, optionally filtered by type. Returns list of {id, name, type}."""
        if entity_type:
            rows = self._conn.execute(
                "SELECT id, name, type FROM entities WHERE type = ? ORDER BY name",
                (entity_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, name, type FROM entities ORDER BY type, name",
            ).fetchall()
        return [dict(r) for r in rows]

    def list_entities_in_scopes(self, scopes: list[str],
                                entity_type: str | None = None) -> list[dict]:
        if not scopes:
            return []
        placeholders = ",".join("?" * len(scopes))
        type_filter = ""
        type_params: list = []
        if entity_type:
            type_filter = "AND e.type = ?"
            type_params = [entity_type]
        # Find entities that have slots OR observations in the given scopes
        rows = self._conn.execute(
            f"SELECT DISTINCT e.id, e.name, e.type FROM entities e "
            f"JOIN slots s ON e.id = s.entity_id "
            f"WHERE s.scope IN ({placeholders}) AND s.valid_to IS NULL {type_filter} "
            f"UNION "
            f"SELECT DISTINCT e.id, e.name, e.type FROM entities e "
            f"JOIN observations o ON e.id = o.entity_id "
            f"WHERE o.scope IN ({placeholders}) AND o.archived_at IS NULL {type_filter} "
            f"ORDER BY name",
            list(scopes) + type_params + list(scopes) + type_params,
        ).fetchall()
        return [dict(r) for r in rows]

    def list_entities_with_observations_in_scope(
        self, scope: str, entity_type: str | None = None,
    ) -> list[dict]:
        """Find entities that have active observations in the given scope."""
        type_filter = ""
        params: list = [scope]
        if entity_type:
            type_filter = "AND e.type = ?"
            params.append(entity_type)
        rows = self._conn.execute(
            f"SELECT DISTINCT e.id, e.name, e.type FROM entities e "
            f"JOIN observations o ON e.id = o.entity_id "
            f"WHERE o.scope = ? AND o.archived_at IS NULL {type_filter} "
            f"ORDER BY e.name", params,
        ).fetchall()
        return [dict(r) for r in rows]

    def find_entities_by_slot(
        self, key: str, value: str | None = None,
        entity_type: str | None = None, scope: str | None = None,
    ) -> list[dict]:
        """Find entities that have a current slot matching the given criteria.

        Single SQL query — avoids N+1 pattern of list_entities + get_slots per entity.

        Args:
            key: Slot key to match (required).
            value: If given, slot value must equal this.
            entity_type: If given, filter entities by type.
            scope: If given, filter slots by scope.

        Returns:
            List of ``{id, name, type}`` dicts for matching entities.
        """
        conditions = ["s.key = ?", "s.valid_to IS NULL"]
        params: list[str] = [key]
        if value is not None:
            conditions.append("s.value = ?")
            params.append(value)
        if entity_type is not None:
            conditions.append("e.type = ?")
            params.append(entity_type)
        if scope is not None:
            conditions.append("s.scope = ?")
            params.append(scope)
        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT DISTINCT e.id, e.name, e.type FROM entities e "
            f"JOIN slots s ON e.id = s.entity_id "
            f"WHERE {where} ORDER BY e.name",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_entity(self, entity_id: int) -> None:
        with self._auto_commit():
            self._conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))

    # --- Slots (scope-aware, bitemporal) ---

    def set_slot(self, entity_id: int, key: str, value: str, scope: str = "global",
                 confidence: float = 1.0, source: str = "agent") -> None:
        """Set a key-value slot on an entity. Bitemporal — old values are archived, not deleted.

        Args:
            entity_id: Target entity.
            key: Slot name (e.g. "role", "email").
            value: Slot value.
            scope: Visibility scope (default "global"). Use hierarchy scopes for isolation.
            confidence: Confidence score 0.0-1.0 (default 1.0).
            source: Who set this value (default "agent").
        """
        now = self._now()
        with self._auto_commit():
            self._conn.execute(
                "UPDATE slots SET valid_to = ? "
                "WHERE entity_id = ? AND key = ? AND scope = ? AND valid_to IS NULL",
                (now, entity_id, key, scope),
            )
            self._conn.execute(
                "INSERT INTO slots (entity_id, key, value, scope, confidence, valid_from, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entity_id, key, value, scope, confidence, now, source),
            )

    def get_slot(self, entity_id: int, key: str,
                 scope_chain: list[str] | None = None) -> str | None:
        """Get a single slot value. With scope_chain, searches from local to global.

        Args:
            entity_id: Target entity.
            key: Slot name.
            scope_chain: Optional list of scopes to search (e.g. ["global", "acme", "proj-a"]).
                         Searches from most specific (last) to most general (first).
        """
        if scope_chain:
            for scope in reversed(scope_chain):
                row = self._conn.execute(
                    "SELECT value FROM slots "
                    "WHERE entity_id = ? AND key = ? AND scope = ? AND valid_to IS NULL",
                    (entity_id, key, scope),
                ).fetchone()
                if row:
                    return row["value"]
            return None
        row = self._conn.execute(
            "SELECT value FROM slots "
            "WHERE entity_id = ? AND key = ? AND valid_to IS NULL "
            "ORDER BY valid_from DESC LIMIT 1",
            (entity_id, key),
        ).fetchone()
        return row["value"] if row else None

    def get_slots(self, entity_id: int,
                  scope_chain: list[str] | None = None) -> dict[str, str]:
        """Get all current slots as {key: value}. With scope_chain, merges scopes (local wins)."""
        if scope_chain:
            merged: dict[str, str] = {}
            for scope in scope_chain:  # global first, local last (overwrites)
                rows = self._conn.execute(
                    "SELECT key, value FROM slots "
                    "WHERE entity_id = ? AND scope = ? AND valid_to IS NULL",
                    (entity_id, scope),
                ).fetchall()
                for r in rows:
                    merged[r["key"]] = r["value"]
            return merged
        rows = self._conn.execute(
            "SELECT key, value FROM slots WHERE entity_id = ? AND valid_to IS NULL",
            (entity_id,),
        ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def archive_slot(self, entity_id: int, key: str,
                     scope: str | None = None) -> None:
        now = self._now()
        with self._auto_commit():
            if scope:
                self._conn.execute(
                    "UPDATE slots SET valid_to = ? "
                    "WHERE entity_id = ? AND key = ? AND scope = ? AND valid_to IS NULL",
                    (now, entity_id, key, scope),
                )
            else:
                self._conn.execute(
                    "UPDATE slots SET valid_to = ? "
                    "WHERE entity_id = ? AND key = ? AND valid_to IS NULL",
                    (now, entity_id, key),
                )

    def get_slot_history(self, entity_id: int, key: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT value, scope, confidence, valid_from, valid_to, source "
            "FROM slots WHERE entity_id = ? AND key = ? ORDER BY valid_from",
            (entity_id, key),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_raw_slots(self, entity_id: int) -> list[dict]:
        """Get all active slots for an entity (unmerged, with scope metadata)."""
        rows = self._conn.execute(
            "SELECT key, value, scope, confidence, source FROM slots "
            "WHERE entity_id = ? AND valid_to IS NULL", (entity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_entity_scopes(self, entity_id: int) -> set[str]:
        """Get all non-global scopes an entity has data in (slots + observations)."""
        obs_rows = self._conn.execute(
            "SELECT DISTINCT scope FROM observations "
            "WHERE entity_id = ? AND archived_at IS NULL",
            (entity_id,),
        ).fetchall()
        slot_rows = self._conn.execute(
            "SELECT DISTINCT scope FROM slots "
            "WHERE entity_id = ? AND valid_to IS NULL",
            (entity_id,),
        ).fetchall()
        scopes = {r["scope"] for r in obs_rows} | {r["scope"] for r in slot_rows}
        scopes.discard("global")
        return scopes

    # --- Observations ---

    def add_observation(self, entity_id: int, text: str,
                        scope: str = "global",
                        dedup: bool = True) -> int:
        """Add a free-text observation to an entity. Returns observation ID.

        When ``dedup=True`` (default), checks for near-duplicates via FTS5
        before inserting. If a similar existing observation is found (same
        entity + FTS5 match), the new text is merged instead of duplicated.

        After insert, initializes a ``knowledge_tiers`` row (tier='warm',
        salience=0.5) so the observation participates in the tier lifecycle.
        """
        now = self._now()
        with self._auto_commit():
            # Dedup: check for similar existing observations on the same entity
            if dedup and self._has_fts:
                existing_similar = self._find_similar_on_entity(entity_id, text)
                if existing_similar is not None:
                    # Merge: keep the longer text
                    existing_id, existing_text = existing_similar
                    if len(text) > len(existing_text):
                        # ponytail: P9 — close valid_time on old version before overwrite
                        self._conn.execute(
                            "UPDATE observations SET valid_to = ? WHERE id = ? AND valid_to IS NULL",
                            (now, existing_id),
                        )
                        self._conn.execute(
                            "UPDATE observations SET text = ? WHERE id = ?",
                            (text, existing_id),
                        )
                    # Update timestamp to reflect fresh access
                    self._conn.execute(
                        "UPDATE observations SET created_at = ? WHERE id = ?",
                        (now, existing_id),
                    )
                    # Still initialize tier if missing (with last_accessed_at=now)
                    self._conn.execute("""
                        INSERT OR IGNORE INTO knowledge_tiers
                            (observation_id, tier, salience_score, last_accessed_at)
                        VALUES (?, 'warm', 0.5, ?)
                    """, (existing_id, now))
                    return existing_id

            cur = self._conn.execute(
                "INSERT INTO observations (entity_id, text, scope, created_at, valid_from) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity_id, text, scope, now, now),
            )
            obs_id = cur.lastrowid

            # Initialize knowledge_tiers row for lifecycle participation.
            # Set last_accessed_at=now so the decay engine doesn't treat
            # a brand-new observation as "never accessed" and demote it to cold.
            self._conn.execute("""
                INSERT OR IGNORE INTO knowledge_tiers
                    (observation_id, tier, salience_score, last_accessed_at)
                VALUES (?, 'warm', 0.5, ?)
            """, (obs_id, now))

            # ponytail: P10 — lazy-embed into vec0 for persistent vector index
            try:
                import sqlite_vec
                self._conn.enable_load_extension(True)
                sqlite_vec.load(self._conn)
                from agent_recall.embeddings import get_provider
                import struct
                p = get_provider()
                if p is not None:
                    vec = p.embed(text)
                    blob = struct.pack(f'{len(vec)}f', *vec)
                    self._conn.execute(
                        "INSERT OR IGNORE INTO observation_embeddings(embedding, observation_id, entity_id) "
                        "VALUES (?, ?, ?)",
                        (blob, obs_id, entity_id),
                    )
            except Exception:
                pass  # Best-effort: vec0 may not be available

        return obs_id

    def _find_similar_on_entity(
        self, entity_id: int, text: str,
    ) -> tuple[int, str] | None:
        """FTS5-based near-duplicate check scoped to a single entity.

        Returns (existing_id, existing_text) if a near-duplicate exists,
        or None if the observation is unique.
        """
        import re
        tokens = re.findall(r'\w{4,}', text.lower())
        if not tokens:
            return None
        fts_terms = " OR ".join(f'"{t}"' for t in tokens[:10])
        row = self._conn.execute(
            "SELECT o.id, o.text FROM observations o "
            "JOIN observations_fts f ON o.id = f.rowid "
            "WHERE observations_fts MATCH ? "
            "  AND o.entity_id = ? "
            "  AND o.archived_at IS NULL "
            "LIMIT 1",
            (fts_terms, entity_id),
        ).fetchone()
        if row:
            return (row["id"], row["text"])
        return None

    def get_observations(self, entity_id: int,
                         include_archived: bool = False) -> list[dict]:
        """Get observations for an entity. Returns list of {id, text, scope, created_at}."""
        if include_archived:
            rows = self._conn.execute(
                "SELECT id, text, scope, created_at, archived_at FROM observations "
                "WHERE entity_id = ? ORDER BY created_at", (entity_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, text, scope, created_at FROM observations "
                "WHERE entity_id = ? AND archived_at IS NULL ORDER BY created_at",
                (entity_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ponytail: P9 — bitemporal snapshot query
    def get_observation_snapshot(self, observation_id: int, at_time: str) -> dict | None:
        """Get what was known about an observation at a point in time."""
        row = self._conn.execute(
            """SELECT id, entity_id, text, scope, created_at, valid_from, valid_to
               FROM observations WHERE id = ?
               AND (valid_from IS NULL OR valid_from <= ?)
               AND (valid_to IS NULL OR valid_to > ?)""",
            (observation_id, at_time, at_time),
        ).fetchone()
        return dict(row) if row else None

    def archive_observation(self, observation_id: int) -> None:
        with self._auto_commit():
            self._conn.execute(
                "UPDATE observations SET archived_at = ? WHERE id = ?",
                (self._now(), observation_id),
            )

    def delete_observation_by_text(self, entity_id: int, text: str) -> int:
        """Archive observations matching text. Returns number of rows affected."""
        with self._auto_commit():
            cur = self._conn.execute(
                "UPDATE observations SET archived_at = ? "
                "WHERE entity_id = ? AND text = ? AND archived_at IS NULL",
                (self._now(), entity_id, text),
            )
        return cur.rowcount

    # --- Relations ---

    def add_relation(self, from_id: int, to_id: int, rel_type: str,
                     scope: str = "global", context: str | None = None) -> int:
        """Create a directed relation between two entities. Returns relation ID."""
        with self._auto_commit():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO relations (from_id, to_id, type, scope, context, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (from_id, to_id, rel_type, scope, context, self._now()),
            )
        return cur.lastrowid

    def archive_relation(self, relation_id: int) -> None:
        with self._auto_commit():
            self._conn.execute(
                "UPDATE relations SET status='former', archived_at=? WHERE id=?",
                (self._now(), relation_id),
            )

    def get_relations(self, entity_id: int,
                      include_archived: bool = False) -> list[dict]:
        if include_archived:
            rows = self._conn.execute(
                "SELECT r.id, r.to_id, e.name as to_name, r.type, r.scope, "
                "r.status, r.context, r.created_at, r.archived_at "
                "FROM relations r JOIN entities e ON r.to_id = e.id "
                "WHERE r.from_id = ? ORDER BY r.created_at", (entity_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT r.id, r.to_id, e.name as to_name, r.type, r.scope, "
                "r.status, r.context, r.created_at "
                "FROM relations r JOIN entities e ON r.to_id = e.id "
                "WHERE r.from_id = ? AND r.status = 'active' ORDER BY r.created_at",
                (entity_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_reverse_relations(self, entity_id: int,
                              rel_type: str | None = None) -> list[dict]:
        if rel_type:
            rows = self._conn.execute(
                "SELECT r.id, r.from_id, e.name as from_name, r.type, r.scope, r.context "
                "FROM relations r JOIN entities e ON r.from_id = e.id "
                "WHERE r.to_id = ? AND r.type = ? AND r.status = 'active'",
                (entity_id, rel_type),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT r.id, r.from_id, e.name as from_name, r.type, r.scope, r.context "
                "FROM relations r JOIN entities e ON r.from_id = e.id "
                "WHERE r.to_id = ? AND r.status = 'active'", (entity_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_relations_by_type(self, rel_type: str) -> list[dict]:
        """Get all active relations of a given type (across all entities)."""
        rows = self._conn.execute(
            "SELECT from_id, to_id FROM relations "
            "WHERE type = ? AND status = 'active'", (rel_type,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_relations(self, entity_id: int) -> tuple[list[dict], list[dict]]:
        """Get all active outgoing and incoming relations for an entity."""
        outgoing = self._conn.execute(
            "SELECT to_id, type, scope, context FROM relations "
            "WHERE from_id = ? AND status = 'active'", (entity_id,),
        ).fetchall()
        incoming = self._conn.execute(
            "SELECT from_id, type, scope, context FROM relations "
            "WHERE to_id = ? AND status = 'active'", (entity_id,),
        ).fetchall()
        return [dict(r) for r in outgoing], [dict(r) for r in incoming]

    @contextmanager
    def _auto_commit(self):
        """Context manager: no-op inside transaction, BEGIN IMMEDIATE otherwise.

        Tracks ``_in_transaction`` to prevent nested ``BEGIN IMMEDIATE``
        when this context manager is re-entered on the same connection
        (e.g. ``add_observation`` inside a loop that also does raw DML).
        """
        if self._in_transaction:
            yield
        else:
            self._conn.execute("BEGIN IMMEDIATE")
            self._in_transaction = True
            try:
                yield
                self._conn.commit()
                self._in_transaction = False
            except BaseException:
                self._conn.rollback()
                self._in_transaction = False
                raise

    def transaction(self) -> _Transaction:
        """Context manager for atomic multi-step operations.

        All store operations within the block share a single transaction.
        Commits on success, rolls back on exception.

        Example::

            with store.transaction():
                store.set_slot(eid, "key", "val")
                store.add_observation(eid, "text")
        """
        return _Transaction(self)

    # --- Log Entries ---

    def add_log(self, entity_id: int, text: str, date: str | None = None,
                author: str = "agent") -> None:
        with self._auto_commit():
            self._conn.execute(
                "INSERT INTO log_entries (entity_id, date, text, author, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity_id, date or self._today(), text, author, self._now()),
            )

    def get_logs(self, entity_id: int, limit: int | None = None) -> list[dict]:
        if limit is not None:
            # Get newest entries, then reverse to chronological order
            sql = ("SELECT date, text, author, created_at FROM log_entries "
                   "WHERE entity_id = ? ORDER BY date DESC, id DESC LIMIT ?")
            rows = self._conn.execute(sql, (entity_id, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]
        sql = ("SELECT date, text, author, created_at FROM log_entries "
               "WHERE entity_id = ? ORDER BY date, id")
        return [dict(r) for r in self._conn.execute(sql, (entity_id,)).fetchall()]

    # --- Documents ---

    def save_document(self, name: str, doc_type: str, content: str,
                      tags: list[str] | None = None) -> None:
        now = self._now()
        tags_json = json.dumps(tags or [])
        with self._auto_commit():
            self._conn.execute(
                "INSERT INTO documents (name, type, content, tags, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET content=?, tags=?, updated_at=?",
                (name, doc_type, content, tags_json, now, now, content, tags_json, now),
            )

    def get_document(self, name: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE name = ?", (name,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["tags"] = json.loads(d["tags"])
        return d

    def list_documents(self, doc_type: str | None = None) -> list[dict]:
        if doc_type:
            rows = self._conn.execute(
                "SELECT name, type, tags, updated_at FROM documents "
                "WHERE type = ? ORDER BY name", (doc_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT name, type, tags, updated_at FROM documents ORDER BY type, name",
            ).fetchall()
        return [dict(r) for r in rows]

    # --- Search ---

    @staticmethod
    def _escape_like(query: str) -> str:
        return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def search(self, query: str, limit: int = 100) -> list[dict]:
        """Full-text search across entity names, slot values, and observations.

        Uses FTS5 with Porter stemmer when available (handles word forms
        like "running" matching "run"). Falls back to LIKE-based search
        for databases without FTS5 tables.

        Returns list of {id, name, type} dicts.
        """
        if self._has_fts:
            return self._search_fts(query, limit)
        return self._search_like(query, limit)

    def _escape_fts(self, term: str) -> str:
        """Escape a term for FTS5 query syntax.

        Wraps in double quotes to treat special characters as literals.
        """
        # Replace double quotes inside the term
        return '"' + term.replace('"', '""') + '"'

    def _search_fts(self, query: str, limit: int) -> list[dict]:
        """FTS5-based search with Porter stemming."""
        found: dict[int, dict] = {}
        words = query.split()
        if not words:
            words = [query]

        # Build FTS5 query: each word as a separate term with OR
        fts_terms = [self._escape_fts(w) for w in words if w.strip()]
        if not fts_terms:
            return []
        fts_query = " OR ".join(fts_terms)

        # Search entity names via FTS
        for r in self._conn.execute(
            "SELECT e.id, e.name, e.type FROM entities e "
            "JOIN entities_fts f ON e.id = f.rowid "
            "WHERE entities_fts MATCH ? LIMIT ?",
            (fts_query, limit),
        ).fetchall():
            found[r["id"]] = dict(r)

        # Search observations via FTS (only non-archived)
        if len(found) < limit:
            for r in self._conn.execute(
                "SELECT DISTINCT e.id, e.name, e.type FROM entities e "
                "JOIN observations o ON e.id = o.entity_id "
                "JOIN observations_fts f ON o.id = f.rowid "
                "WHERE observations_fts MATCH ? "
                "AND o.archived_at IS NULL LIMIT ?",
                (fts_query, limit),
            ).fetchall():
                found[r["id"]] = dict(r)

        # Slot values are not in FTS — fall back to LIKE for slots
        if len(found) < limit:
            for word in words:
                if len(found) >= limit:
                    break
                pattern = f"%{self._escape_like(word)}%"
                for r in self._conn.execute(
                    "SELECT DISTINCT e.id, e.name, e.type FROM entities e "
                    "JOIN slots s ON e.id = s.entity_id "
                    "WHERE s.value LIKE ? ESCAPE '\\' AND s.valid_to IS NULL LIMIT ?",
                    (pattern, limit),
                ).fetchall():
                    found[r["id"]] = dict(r)

        return list(found.values())[:limit]

    def _search_like(self, query: str, limit: int) -> list[dict]:
        """LIKE-based fallback search for databases without FTS5."""
        found: dict[int, dict] = {}
        words = query.split()
        if not words:
            words = [query]
        patterns: list[str] = []
        for word in words:
            patterns.append(f"%{self._escape_like(word)}%")
            if len(word) > 5:
                stem_len = max(4, int(len(word) * 0.6))
                stem = word[:stem_len]
                patterns.append(f"%{self._escape_like(stem)}%")
        for pattern in patterns:
            if len(found) >= limit:
                break
            for r in self._conn.execute(
                "SELECT id, name, type FROM entities "
                "WHERE name LIKE ? ESCAPE '\\' LIMIT ?",
                (pattern, limit),
            ).fetchall():
                found[r["id"]] = dict(r)
            for r in self._conn.execute(
                "SELECT DISTINCT e.id, e.name, e.type FROM entities e "
                "JOIN slots s ON e.id = s.entity_id "
                "WHERE s.value LIKE ? ESCAPE '\\' AND s.valid_to IS NULL LIMIT ?",
                (pattern, limit),
            ).fetchall():
                found[r["id"]] = dict(r)
            for r in self._conn.execute(
                "SELECT DISTINCT e.id, e.name, e.type FROM entities e "
                "JOIN observations o ON e.id = o.entity_id "
                "WHERE o.text LIKE ? ESCAPE '\\' AND o.archived_at IS NULL LIMIT ?",
                (pattern, limit),
            ).fetchall():
                found[r["id"]] = dict(r)
        return list(found.values())[:limit]

    def count_scope(self, scope: str) -> dict[str, int]:
        """Count active data in a scope. Returns {slots, observations, relations}."""
        slots = self._conn.execute(
            "SELECT COUNT(*) FROM slots WHERE scope = ? AND valid_to IS NULL",
            (scope,)).fetchone()[0]
        obs = self._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE scope = ? AND archived_at IS NULL",
            (scope,)).fetchone()[0]
        rels = self._conn.execute(
            "SELECT COUNT(*) FROM relations WHERE scope = ? AND status = 'active'",
            (scope,)).fetchone()[0]
        return {"slots": slots, "observations": obs, "relations": rels}

    # --- Integrity Checks ---

    def find_orphaned_scopes(self, valid_scopes: set[str]) -> list[str]:
        """Find scopes present in DB but not in the provided valid set.

        Checks both slots and observations tables.

        Args:
            valid_scopes: Set of known-good scope strings.

        Returns:
            Sorted list of orphaned scope strings.
        """
        db_scopes: set[str] = set()
        for row in self._conn.execute("SELECT DISTINCT scope FROM slots"):
            db_scopes.add(row[0])
        for row in self._conn.execute("SELECT DISTINCT scope FROM observations"):
            db_scopes.add(row[0])
        return sorted(db_scopes - valid_scopes)

    def find_duplicate_slots(self, entity_type: str | None = None) -> list[dict]:
        """Find entities with duplicate current slots (same entity_id, key, scope).

        Only considers active slots (valid_to IS NULL). Multiple active slots
        for the same (entity, key, scope) indicate a data integrity issue.

        Args:
            entity_type: If given, only check entities of this type.

        Returns:
            List of ``{entity_id, entity_name, key, scope, count}`` dicts.
        """
        type_filter = ""
        params: list[str] = []
        if entity_type is not None:
            type_filter = "AND e.type = ?"
            params.append(entity_type)
        rows = self._conn.execute(
            f"SELECT s.entity_id as entity_id, e.name as entity_name, "
            f"s.key as key, s.scope as scope, COUNT(*) as count "
            f"FROM slots s JOIN entities e ON s.entity_id = e.id "
            f"WHERE s.valid_to IS NULL {type_filter} "
            f"GROUP BY s.entity_id, s.key, s.scope "
            f"HAVING count > 1 ORDER BY count DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def find_thin_entities(
        self, exclude_types: set[str] | None = None,
    ) -> list[dict]:
        """Find entities with minimal data (<=1 slot and 0 observations).

        Useful for identifying placeholder or orphaned entities.

        Args:
            exclude_types: Entity types to skip (e.g. {"draft", "topic"}).

        Returns:
            List of ``{entity_id, name, type, slots, observations}`` dicts.
        """
        exclude = exclude_types or set()
        placeholders = ",".join("?" * len(exclude)) if exclude else ""
        type_filter = f"AND e.type NOT IN ({placeholders})" if exclude else ""
        rows = self._conn.execute(
            f"SELECT e.id as entity_id, e.name as name, e.type as type, "
            f"  COALESCE(s.cnt, 0) as slots, "
            f"  COALESCE(o.cnt, 0) as observations "
            f"FROM entities e "
            f"LEFT JOIN (SELECT entity_id, COUNT(*) as cnt FROM slots "
            f"  WHERE valid_to IS NULL GROUP BY entity_id) s ON e.id = s.entity_id "
            f"LEFT JOIN (SELECT entity_id, COUNT(*) as cnt FROM observations "
            f"  WHERE archived_at IS NULL GROUP BY entity_id) o ON e.id = o.entity_id "
            f"WHERE COALESCE(s.cnt, 0) <= 1 AND COALESCE(o.cnt, 0) = 0 "
            f"  {type_filter} "
            f"ORDER BY e.type, e.name",
            list(exclude),
        ).fetchall()
        return [dict(r) for r in rows]

    def check_integrity(
        self, valid_scopes: set[str] | None = None,
        exclude_types: set[str] | None = None,
    ) -> dict:
        """Run all integrity checks. Returns summary dict.

        Args:
            valid_scopes: Set of known-good scopes. If None, orphan check is skipped.
            exclude_types: Entity types to skip in thin-entity check.
                Defaults to ``{"draft", "topic"}``.

        Returns:
            Dict with keys: ``orphaned_scopes``, ``duplicate_slots``, ``thin_entities``.
            Each value is a list (empty if no issues).
        """
        if exclude_types is None:
            exclude_types = {"draft", "topic"}
        result: dict[str, list] = {
            "orphaned_scopes": [],
            "duplicate_slots": [],
            "thin_entities": [],
        }
        if valid_scopes is not None:
            result["orphaned_scopes"] = self.find_orphaned_scopes(valid_scopes)
        result["duplicate_slots"] = self.find_duplicate_slots()
        result["thin_entities"] = self.find_thin_entities(
            exclude_types=exclude_types)
        return result

    def rename_scope(self, old: str, new: str) -> dict:
        """Migrate all data from one scope to another.

        Updates scope on all current slots, active observations, and active
        relations that have the old scope. Returns counts of affected rows.

        Args:
            old: The scope to rename from.
            new: The scope to rename to.

        Returns:
            Dict with keys ``slots``, ``observations``, ``relations`` giving
            the number of rows updated in each table.

        Raises:
            ValueError: If scope names are empty, equal, or ``old`` is "global".
        """
        if not old or not new:
            raise ValueError("Scope names cannot be empty")
        if old == new:
            raise ValueError("Old and new scope must differ")
        if old == "global":
            raise ValueError("Cannot rename the global scope")
        counts = {}
        with self._auto_commit():
            cur = self._conn.execute(
                "UPDATE slots SET scope = ? WHERE scope = ? AND valid_to IS NULL",
                (new, old),
            )
            counts["slots"] = cur.rowcount
            cur = self._conn.execute(
                "UPDATE observations SET scope = ? WHERE scope = ? AND archived_at IS NULL",
                (new, old),
            )
            counts["observations"] = cur.rowcount
            cur = self._conn.execute(
                "UPDATE relations SET scope = ? WHERE scope = ? AND status = 'active'",
                (new, old),
            )
            counts["relations"] = cur.rowcount
        return counts

    # ------------------------------------------------------------------
    # Tier management (v0.5.0)
    # ------------------------------------------------------------------

    def set_tier(self, observation_id: int, tier: str,
                 source: str = "manual") -> None:
        """Set or update the knowledge tier for an observation."""
        now = self._now()
        with self._auto_commit():
            self._conn.execute("""
                INSERT INTO knowledge_tiers
                    (observation_id, tier, promoted_at, promotion_source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    tier = ?, promoted_at = ?, promotion_source = ?
            """, (observation_id, tier, now, source, tier, now, source))

    def get_tier(self, observation_id: int) -> str | None:
        """Get the current tier for an observation."""
        row = self._conn.execute(
            "SELECT tier FROM knowledge_tiers WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return row["tier"] if row else None

    def get_hot_cache(self, scope: str, limit: int = 20) -> list[dict]:
        """Return hot-tier observations in a scope, sorted by salience."""
        rows = self._conn.execute("""
            SELECT o.id, o.entity_id, o.text, o.scope, o.created_at,
                   kt.tier, kt.salience_score, kt.access_count,
                   kt.last_accessed_at, e.name as entity_name, e.type as entity_type
            FROM observations o
            JOIN knowledge_tiers kt ON o.id = kt.observation_id
            JOIN entities e ON o.entity_id = e.id
            WHERE kt.tier = 'hot' AND o.scope = ? AND o.archived_at IS NULL
            ORDER BY kt.salience_score DESC
            LIMIT ?
        """, (scope, limit)).fetchall()
        return [dict(r) for r in rows]

    def update_access(self, observation_id: int) -> None:
        """Increment access_count and update last_accessed_at.

        Also triggers salience recomputation.
        """
        now = self._now()
        with self._auto_commit():
            self._conn.execute("""
                INSERT INTO knowledge_tiers
                    (observation_id, tier, access_count, last_accessed_at)
                VALUES (?, 'warm', 1, ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    access_count = access_count + 1,
                    last_accessed_at = ?
            """, (observation_id, now, now))
        self._maybe_maintenance()

    def _maybe_maintenance(self) -> None:
        """Trigger full maintenance cycle every ~100 writes or after 1 hour.

        Runs: tier rebalance + knowledge promotion + synthesis + trust decay.
        """
        self._write_count += 1
        if self._write_count % 100 != 0:
            return
        elapsed = (datetime.now(timezone.utc) - self._last_rebalance).total_seconds()
        if elapsed < 3600:
            return
        try:
            from agent_recall.knowledge_tiers import KnowledgeTierManager
            mgr = KnowledgeTierManager(self)
            mgr.run_full_maintenance()
            self._last_rebalance = datetime.now(timezone.utc)
        except Exception:
            pass  # Maintenance is best-effort; never break the store

    def bulk_update_salience(self) -> int:
        """Recompute salience for all observations. Returns number updated."""
        from agent_recall.decay_engine import DecayEngine
        engine = DecayEngine()
        result = engine.bulk_update_tiers(self._conn)
        self._conn.commit()
        return result["promoted"] + result["demoted"]

    # ------------------------------------------------------------------
    # Token Budget (v0.5.0)
    # ------------------------------------------------------------------

    def get_token_budget(self, scope: str) -> int | None:
        """Get token budget for a scope. Returns None if not set."""
        row = self._conn.execute(
            "SELECT budget_tokens FROM token_budgets WHERE scope = ?",
            (scope,),
        ).fetchone()
        return row["budget_tokens"] if row else None

    def set_token_budget(self, scope: str, tokens: int) -> None:
        """Set token budget for a scope."""
        with self._auto_commit():
            self._conn.execute("""
                INSERT INTO token_budgets (scope, budget_tokens, used_tokens, last_reset)
                VALUES (?, ?, 0, datetime('now'))
                ON CONFLICT(scope) DO UPDATE SET budget_tokens = ?
            """, (scope, tokens, tokens))

    # ------------------------------------------------------------------
    # Pattern Store (v0.5.0)
    # ------------------------------------------------------------------

    def upsert_pattern(self, text: str, pattern_type: str,
                       confidence: float = 0.5,
                       source_entity_type: str | None = None,
                       metadata: dict | None = None) -> int:
        """Insert or update a pattern. Returns pattern ID."""
        import hashlib
        pattern_hash = hashlib.sha256(text.encode()).hexdigest()
        now = self._now()
        meta_json = json.dumps(metadata or {})
        with self._auto_commit():
            self._conn.execute("""
                INSERT INTO pattern_store
                    (pattern_hash, pattern_text, pattern_type, first_seen, last_seen,
                     confidence, source_entity_type, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pattern_hash) DO UPDATE SET
                    occurrence_count = occurrence_count + 1,
                    last_seen = ?,
                    confidence = MAX(confidence, ?)
            """, (pattern_hash, text, pattern_type, now, now,
                  confidence, source_entity_type, meta_json,
                  now, confidence))
            row = self._conn.execute(
                "SELECT id FROM pattern_store WHERE pattern_hash = ?",
                (pattern_hash,),
            ).fetchone()
        return row["id"] if row else -1

    def get_patterns(self, pattern_type: str | None = None,
                     min_count: int = 1) -> list[dict]:
        """List patterns, optionally filtered by type and minimum occurrence."""
        if pattern_type:
            rows = self._conn.execute(
                "SELECT * FROM pattern_store "
                "WHERE pattern_type = ? AND occurrence_count >= ? "
                "ORDER BY confidence DESC",
                (pattern_type, min_count),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM pattern_store "
                "WHERE occurrence_count >= ? "
                "ORDER BY confidence DESC",
                (min_count,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_promotion_candidates(self, threshold: int = 3) -> list[dict]:
        """Get patterns that appear >= threshold times, sorted by confidence."""
        rows = self._conn.execute(
            "SELECT * FROM pattern_store WHERE occurrence_count >= ? "
            "ORDER BY confidence DESC",
            (threshold,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Retrieval Events (v0.5.0)
    # ------------------------------------------------------------------

    def log_retrieval(self, query: str, observation_id: int,
                      similarity: float) -> int:
        """Log a retrieval event. Returns event ID."""
        import hashlib
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        with self._auto_commit():
            cur = self._conn.execute(
                "INSERT INTO retrieval_events (query_hash, observation_id, similarity) "
                "VALUES (?, ?, ?)",
                (query_hash, observation_id, similarity),
            )
        return cur.lastrowid

    def log_usage(self, retrieval_id: int, was_used: bool,
                  feedback: str | None = None) -> None:
        """Record whether a retrieved observation was actually used."""
        with self._auto_commit():
            self._conn.execute(
                "UPDATE retrieval_events SET was_used = ?, feedback = ? "
                "WHERE id = ?",
                (1 if was_used else -1, feedback, retrieval_id),
            )

    def get_helpfulness(self, observation_id: int) -> float:
        """Bayesian helpfulness: (used+α)/(retrieved+α+β), α=1, β=3."""
        row = self._conn.execute(
            "SELECT "
            "  COUNT(*) as total, "
            "  SUM(CASE WHEN was_used = 1 THEN 1 ELSE 0 END) as used "
            "FROM retrieval_events WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        if not row or row["total"] == 0:
            return 0.25  # conservative prior: 1/(1+3)
        total = row["total"]
        used = row["used"] or 0
        alpha, beta = 1.0, 3.0
        return (used + alpha) / (total + alpha + beta)

    # ------------------------------------------------------------------
    # Trust (v0.5.0)
    # ------------------------------------------------------------------

    def get_trust_score(self, observation_id: int) -> float:
        """Get current trust score. Initial 1.0 (innocent until proven guilty).

        Returns the latest trust_events.new_trust if available,
        otherwise 1.0 for observations with no trust history.
        Range: [0.0, 1.0].
        """
        row = self._conn.execute(
            "SELECT new_trust FROM trust_events "
            "WHERE observation_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (observation_id,),
        ).fetchone()
        if row:
            return row["new_trust"]
        # No trust events → full trust (1.0), not base_importance (0.5)
        return 1.0

    def adjust_trust(self, observation_id: int, reason: str, delta: float,
                     note: str | None = None) -> float:
        """Adjust trust score and record the event. Returns new score."""
        old = self.get_trust_score(observation_id)
        new = max(0.0, min(1.0, old + delta))
        with self._auto_commit():
            self._conn.execute(
                "INSERT INTO trust_events "
                "(observation_id, reason, old_trust, new_trust, delta, note) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (observation_id, reason, old, new, delta, note),
            )
            # Also update base_importance in knowledge_tiers
            self._conn.execute("""
                INSERT INTO knowledge_tiers
                    (observation_id, tier, base_importance)
                VALUES (?, 'warm', ?)
                ON CONFLICT(observation_id) DO UPDATE SET
                    base_importance = ?
            """, (observation_id, new, new))
        return new

    def get_trust_history(self, observation_id: int) -> list[dict]:
        """Get full trust adjustment history for an observation."""
        rows = self._conn.execute(
            "SELECT reason, old_trust, new_trust, delta, note, created_at "
            "FROM trust_events WHERE observation_id = ? "
            "ORDER BY created_at",
            (observation_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Privacy (v0.5.0)
    # ------------------------------------------------------------------

    def set_privacy(self, observation_id: int, level: str) -> None:
        """Set privacy level for an observation."""
        with self._auto_commit():
            self._conn.execute("""
                INSERT INTO observation_privacy (observation_id, privacy_level, tagged_by)
                VALUES (?, ?, 'agent')
                ON CONFLICT(observation_id) DO UPDATE SET privacy_level = ?
            """, (observation_id, level, level))

    def get_privacy(self, observation_id: int) -> str:
        """Get privacy level for an observation (default 'public')."""
        row = self._conn.execute(
            "SELECT privacy_level FROM observation_privacy WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        return row["privacy_level"] if row else "public"

    # ------------------------------------------------------------------
    # Access Patterns (v0.5.0)
    # ------------------------------------------------------------------

    def record_transition(self, from_id: int, to_id: int) -> None:
        """Record a navigation transition between two observations."""
        now = self._now()
        with self._auto_commit():
            self._conn.execute("""
                INSERT INTO access_patterns
                    (from_observation_id, to_observation_id, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(from_observation_id, to_observation_id) DO UPDATE SET
                    transition_count = transition_count + 1,
                    last_seen = ?
            """, (from_id, to_id, now, now))
            # Recompute probability
            total_row = self._conn.execute(
                "SELECT SUM(transition_count) FROM access_patterns "
                "WHERE from_observation_id = ?",
                (from_id,),
            ).fetchone()
            total = total_row[0] if total_row and total_row[0] else 1
            self._conn.execute(
                "UPDATE access_patterns SET probability = "
                "CAST(transition_count AS REAL) / ? "
                "WHERE from_observation_id = ? AND to_observation_id = ?",
                (total, from_id, to_id),
            )

    def predict_next(self, current_id: int, top_k: int = 5) -> list[dict]:
        """Predict most likely next observations via Markov transition."""
        rows = self._conn.execute("""
            SELECT ap.to_observation_id, ap.probability, ap.transition_count,
                   o.text, e.name as entity_name
            FROM access_patterns ap
            JOIN observations o ON ap.to_observation_id = o.id
            JOIN entities e ON o.entity_id = e.id
            WHERE ap.from_observation_id = ?
              AND o.archived_at IS NULL
            ORDER BY ap.probability DESC
            LIMIT ?
        """, (current_id, top_k)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Timeline (v0.5.0)
    # ------------------------------------------------------------------

    def get_timeline(self, entity_name: str | None = None,
                     entity_type: str | None = None,
                     scope: str | None = None,
                     since: str | None = None,
                     until: str | None = None,
                     limit: int = 20) -> list[dict]:
        """Get observations as a chronological timeline with optional filters."""
        conditions = ["o.archived_at IS NULL"]
        params: list = []

        if entity_name:
            conditions.append("e.name = ?")
            params.append(entity_name)
        if entity_type:
            conditions.append("e.type = ?")
            params.append(entity_type)
        if scope:
            conditions.append("o.scope = ?")
            params.append(scope)
        if since:
            conditions.append("o.created_at >= ?")
            params.append(since)
        if until:
            conditions.append("o.created_at <= ?")
            params.append(until)

        where = " AND ".join(conditions)
        rows = self._conn.execute(
            f"SELECT o.id, o.text, o.scope, o.created_at, "
            f"e.name as entity_name, e.type as entity_type "
            f"FROM observations o "
            f"JOIN entities e ON o.entity_id = e.id "
            f"WHERE {where} "
            f"ORDER BY o.created_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Semantic Dedup (v0.5.0)
    # ------------------------------------------------------------------

    def find_similar_observations(self, text: str, threshold: float = 0.85,
                                  scope: str | None = None) -> list[dict]:
        """Find semantically similar existing observations (FTS5-based).

        Uses FTS5 for rough matching. For vector-based similarity,
        use ``search_vector`` after embedding.
        """
        if not self._has_fts:
            return []
        # Extract key terms for FTS query
        import re
        tokens = re.findall(r'\w{4,}', text.lower())
        if not tokens:
            return []
        fts_terms = " OR ".join(
            f'"{t}"' for t in tokens[:10]
        )
        scope_filter = "AND o.scope = ?" if scope else ""
        params: list = [threshold] if not scope else [scope, threshold]
        # threshold not directly used in FTS, we trust FTS ranking
        rows = self._conn.execute(
            f"SELECT o.id, o.entity_id, o.text, o.scope, o.created_at, "
            f"e.name as entity_name "
            f"FROM observations o "
            f"JOIN observations_fts f ON o.id = f.rowid "
            f"JOIN entities e ON o.entity_id = e.id "
            f"WHERE observations_fts MATCH ? "
            f"  AND o.archived_at IS NULL "
            f"  {scope_filter} "
            f"LIMIT 10",
            params if scope else [threshold],
        ).fetchall()
        return [dict(r) for r in rows]

    def merge_observations(self, keep_id: int, merge_id: int) -> None:
        """Merge merge_id into keep_id: archive merge_id, keep longer text."""
        if keep_id == merge_id:
            return
        with self._auto_commit():
            # Get texts
            keep = self._conn.execute(
                "SELECT text FROM observations WHERE id = ?", (keep_id,)
            ).fetchone()
            merge = self._conn.execute(
                "SELECT text FROM observations WHERE id = ?", (merge_id,)
            ).fetchone()
            if keep and merge and len(merge["text"]) > len(keep["text"]):
                self._conn.execute(
                    "UPDATE observations SET text = ? WHERE id = ?",
                    (merge["text"], keep_id),
                )
            # Archive merge_id
            self._conn.execute(
                "UPDATE observations SET archived_at = ? WHERE id = ?",
                (self._now(), merge_id),
            )
            # Update relations pointing to merge_id → keep_id
            self._conn.execute(
                "UPDATE relations SET to_id = ? WHERE to_id = ?",
                (keep_id, merge_id),
            )

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
