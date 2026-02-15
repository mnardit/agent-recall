"""Vault Generator — creates Obsidian-compatible .md files from frames.db.

Optional output plugin. Only active if vault_dir is configured.
"""
import subprocess
import time
from pathlib import Path

from claude_memory.store import MemoryStore

DEFAULT_RATE_SECONDS = 300  # 5 min


SKIP_SLOTS = {"name", "clients"}
DISPLAY_SLOTS = ["role", "company", "email", "phone", "language",
                 "location", "timezone"]


def trigger_vault_regen(store: MemoryStore | None = None,
                        vault_dir: Path | None = None,
                        db_path: Path | None = None,
                        auto_commit: bool = True,
                        rate_seconds: int = DEFAULT_RATE_SECONDS,
                        rate_file: Path | None = None,
                        force: bool = False) -> bool:
    """Regenerate vault from frames.db with rate limiting.

    Returns True if vault was regenerated, False if rate-limited or no vault.
    """
    if vault_dir is None or not vault_dir.exists():
        return False

    rf = rate_file or Path("/tmp/claude-memory-vault-regen-last")

    # Rate limit (skip if forced)
    if not force and rf.exists():
        last = rf.stat().st_mtime
        if time.time() - last < rate_seconds:
            return False

    rf.write_text(str(time.time()))

    # Open store if not provided
    close_store = False
    if store is None:
        if db_path is None:
            from claude_memory.config import DEFAULT_DB_PATH
            db_path = DEFAULT_DB_PATH
        store = MemoryStore(db_path)
        close_store = True

    try:
        generate_vault(store, vault_dir)
    finally:
        if close_store:
            store.close()

    if auto_commit:
        import shlex
        subprocess.Popen(
            ["bash", "-c",
             f"cd {shlex.quote(str(vault_dir))} && "
             "git add people/ clients/ decisions/ && "
             "git diff --cached --quiet || "
             "git commit -m 'auto: regenerate from frames.db' && git push"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    return True


def _format_slots(slots: dict[str, str], ordered_keys: list[str] | None = None) -> list[str]:
    lines = []
    keys = ordered_keys or sorted(slots.keys())
    for k in keys:
        if k in SKIP_SLOTS or k not in slots:
            continue
        label = k.replace("_", " ").title()
        lines.append(f"- **{label}:** {slots[k]}")
    if ordered_keys:
        for k in sorted(slots.keys()):
            if k not in SKIP_SLOTS and k not in ordered_keys:
                label = k.replace("_", " ").title()
                lines.append(f"- **{label}:** {slots[k]}")
    return lines


def generate_person(store: MemoryStore, entity_id: int, vault_dir: Path) -> Path:
    entity = store.get_entity(entity_id)
    slots = store.get_slots(entity_id)
    name = entity["name"]
    clients = slots.get("clients", "")
    clients_list = [c.strip() for c in clients.split(",") if c.strip()] if clients else []

    people_dir = vault_dir / "people"
    people_dir.mkdir(parents=True, exist_ok=True)
    path = people_dir / f"{name}.md"

    lines = [
        "---",
        "tags: [contact]",
        f"clients: [{', '.join(clients_list)}]",
        "---",
        f"# {name}", "",
    ]

    slot_lines = _format_slots(slots, DISPLAY_SLOTS)
    if slot_lines:
        lines.extend(slot_lines)

    observations = store.get_observations(entity_id)
    if observations:
        lines += ["", "## Notes"]
        for obs in observations:
            lines.append(f"- {obs['text']}")

    rels = store.get_relations(entity_id)
    if rels:
        lines += ["", "## Relations"]
        for r in rels:
            status = " (former)" if r["status"] == "former" else ""
            lines.append(f"- {r['type']}: [[{r['to_name']}]]{status}")

    lines.append("")
    path.write_text("\n".join(lines))
    return path


def generate_client(store: MemoryStore, entity_id: int, vault_dir: Path) -> Path:
    entity = store.get_entity(entity_id)
    slots = store.get_slots(entity_id)
    name = entity["name"]

    clients_dir = vault_dir / "clients"
    clients_dir.mkdir(parents=True, exist_ok=True)
    path = clients_dir / f"{name}.md"

    lines = [
        "---",
        "tags: [client]",
        f"status: {slots.get('status', 'active')}",
        "---",
        f"# {name}", "",
    ]
    lines.extend(_format_slots(slots))

    contacts = store.get_reverse_relations(entity_id, "contact_for")
    if contacts:
        lines += ["", "## Contacts"]
        for c in contacts:
            from_slots = store.get_slots(c["from_id"])
            lines.append(f"- [[{c['from_name']}]] — {from_slots.get('role', '')}")

    logs = store.get_logs(entity_id)
    if logs:
        lines += ["", "## Log"]
        for log in logs:
            lines.append(f"- {log['date']}: {log['text']}")

    lines.append("")
    path.write_text("\n".join(lines))
    return path


def generate_vault(store: MemoryStore, vault_dir: Path) -> dict[str, int]:
    stats = {"people": 0, "clients": 0, "decisions": 0}
    for e in store.list_entities(entity_type="person"):
        generate_person(store, e["id"], vault_dir)
        stats["people"] += 1
    for e in store.list_entities(entity_type="client"):
        generate_client(store, e["id"], vault_dir)
        stats["clients"] += 1
    for doc in store.list_documents(doc_type="decision"):
        full = store.get_document(doc["name"])
        if full:
            dec_dir = vault_dir / "decisions"
            dec_dir.mkdir(parents=True, exist_ok=True)
            (dec_dir / f"{doc['name']}.md").write_text(full["content"])
            stats["decisions"] += 1
    return stats
