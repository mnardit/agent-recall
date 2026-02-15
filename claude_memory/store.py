"""State Layer — single SQLite database for all agent memory.

Single DB with scope column on slots for hierarchical filtering.
Tables: entities, slots (bitemporal+scoped), observations, relations, log_entries, documents.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class MemoryStore:
    def __init__(self, db_path: Path | str, timeout: float = 10.0) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), timeout=timeout)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

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
        """)
        self._conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Entities ---

    def create_entity(self, name: str, entity_type: str) -> int:
        if not name or not name.strip():
            raise ValueError("Entity name cannot be empty")
        if not entity_type or not entity_type.strip():
            raise ValueError("Entity type cannot be empty")
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO entities (name, type, created_at) VALUES (?, ?, ?)",
                (name, entity_type, self._now()),
            )
        return cur.lastrowid

    def find_entity(self, name: str, entity_type: str | None = None) -> int | None:
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

    def resolve_entity(self, name: str, entity_type: str) -> int:
        if not name or not name.strip():
            raise ValueError("Entity name cannot be empty")
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO entities (name, type, created_at) VALUES (?, ?, ?)",
                (name, entity_type, self._now()),
            )
        return self.find_entity(name, entity_type)

    def get_entity(self, entity_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT id, name, type, created_at FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_entities(self, entity_type: str | None = None) -> list[dict]:
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
        placeholders = ",".join("?" * len(scopes))
        params: list = list(scopes)
        type_filter = ""
        if entity_type:
            type_filter = "AND e.type = ?"
            params.append(entity_type)
        rows = self._conn.execute(
            f"SELECT DISTINCT e.id, e.name, e.type FROM entities e "
            f"JOIN slots s ON e.id = s.entity_id "
            f"WHERE s.scope IN ({placeholders}) AND s.valid_to IS NULL {type_filter} "
            f"ORDER BY e.name", params,
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

    def delete_entity(self, entity_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))

    # --- Slots (scope-aware, bitemporal) ---

    def set_slot(self, entity_id: int, key: str, value: str, scope: str = "global",
                 confidence: float = 1.0, source: str = "agent") -> None:
        now = self._now()
        with self._conn:
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
        with self._conn:
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
                        scope: str = "global") -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO observations (entity_id, text, scope, created_at) "
                "VALUES (?, ?, ?, ?)",
                (entity_id, text, scope, self._now()),
            )
        return cur.lastrowid

    def get_observations(self, entity_id: int,
                         include_archived: bool = False) -> list[dict]:
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

    def archive_observation(self, observation_id: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE observations SET archived_at = ? WHERE id = ?",
                (self._now(), observation_id),
            )

    def delete_observation_by_text(self, entity_id: int, text: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE observations SET archived_at = ? "
                "WHERE entity_id = ? AND text = ? AND archived_at IS NULL",
                (self._now(), entity_id, text),
            )

    # --- Relations ---

    def add_relation(self, from_id: int, to_id: int, rel_type: str,
                     scope: str = "global", context: str | None = None) -> int:
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO relations (from_id, to_id, type, scope, context, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (from_id, to_id, rel_type, scope, context, self._now()),
            )
        return cur.lastrowid

    def archive_relation(self, relation_id: int) -> None:
        with self._conn:
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

    def commit(self) -> None:
        """Explicit commit for multi-step operations."""
        self._conn.commit()

    # --- Log Entries ---

    def add_log(self, entity_id: int, text: str, date: str | None = None,
                author: str = "agent") -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO log_entries (entity_id, date, text, author, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (entity_id, date or self._today(), text, author, self._now()),
            )

    def get_logs(self, entity_id: int, limit: int | None = None) -> list[dict]:
        sql = ("SELECT date, text, author, created_at FROM log_entries "
               "WHERE entity_id = ? ORDER BY date, id")
        params: tuple = (entity_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (entity_id, limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # --- Documents ---

    def save_document(self, name: str, doc_type: str, content: str,
                      tags: list[str] | None = None) -> None:
        now = self._now()
        tags_json = json.dumps(tags or [])
        with self._conn:
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
        found: dict[int, dict] = {}
        # Split into words, search each; for long words also try stem
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

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()
