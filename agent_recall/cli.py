"""CLI for agent-recall — manage agent memory from the terminal."""
import json
import sys
from pathlib import Path

import click

from agent_recall.config import MemoryConfig, load_config, DEFAULT_DB_PATH
from agent_recall.store import MemoryStore


def _store(config: MemoryConfig) -> MemoryStore:
    return MemoryStore(config.db_path)


@click.group()
@click.option("--config", "config_path", type=click.Path(exists=True),
              help="Path to memory.yaml config file.")
@click.option("--db", type=click.Path(), help="Override database path.")
@click.pass_context
def main(ctx, config_path, db):
    """agent-recall — persistent memory with scope hierarchy for AI agents."""
    config = load_config(config_path)
    if db:
        config.db_path = Path(db)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config


# --- init ---

@main.command()
@click.option("--db", type=click.Path(), default=None,
              help=f"Database path (default: {DEFAULT_DB_PATH})")
@click.pass_context
def init(ctx, db):
    """Initialize memory database and config."""
    config = ctx.obj["config"]
    db_path = Path(db) if db else config.db_path
    with MemoryStore(db_path):
        pass
    click.echo(f"Database initialized: {db_path}")


# --- set ---

@main.command("set")
@click.argument("entity")
@click.argument("entity_type")
@click.argument("key")
@click.argument("value")
@click.option("--scope", default="global", help="Scope for the slot.")
@click.pass_context
def set_slot(ctx, entity, entity_type, key, value, scope):
    """Set a slot value on an entity."""
    with _store(ctx.obj["config"]) as store:
        eid = store.resolve_entity(entity, entity_type)
        store.set_slot(eid, key, value, scope=scope)
        click.echo(f"{entity}.{key} = {value}")


# --- get ---

@main.command("get")
@click.argument("entity")
@click.argument("key")
@click.pass_context
def get_slot(ctx, entity, key):
    """Get a slot value from an entity."""
    with _store(ctx.obj["config"]) as store:
        eid = store.find_entity(entity)
        if eid is None:
            click.echo("Entity not found", err=True)
            sys.exit(1)
        val = store.get_slot(eid, key)
        if val is None:
            click.echo(f"Slot '{key}' not found on entity '{entity}'", err=True)
            sys.exit(1)
        click.echo(val)


# --- entity ---

@main.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def entity(ctx, name, as_json):
    """Show entity details."""
    with _store(ctx.obj["config"]) as store:
        eid = store.find_entity(name)
        if eid is None:
            click.echo("Not found", err=True)
            sys.exit(1)
        e = store.get_entity(eid)
        slots = store.get_slots(eid)
        if as_json:
            click.echo(json.dumps({"id": e["id"], "name": e["name"],
                                   "type": e["type"], "slots": slots},
                                  ensure_ascii=False, indent=2))
        else:
            click.echo(f"{e['name']} ({e['type']})")
            for k, v in slots.items():
                click.echo(f"  {k}: {v}")


# --- list ---

@main.command("list")
@click.option("--type", "entity_type", help="Filter by entity type.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def list_entities(ctx, entity_type, as_json):
    """List entities in the knowledge graph."""
    with _store(ctx.obj["config"]) as store:
        entities = store.list_entities(entity_type=entity_type)
        if as_json:
            click.echo(json.dumps(entities, ensure_ascii=False, indent=2))
        else:
            for e in entities:
                click.echo(f"  {e['name']} ({e['type']})")


# --- search ---

@main.command()
@click.argument("query")
@click.pass_context
def search(ctx, query):
    """Search entities by name, slot, or observation."""
    with _store(ctx.obj["config"]) as store:
        results = store.search(query)
        for r in results:
            click.echo(f"  {r['name']} ({r['type']})")


# --- history ---

@main.command()
@click.argument("entity")
@click.argument("key")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def history(ctx, entity, key, as_json):
    """Show bitemporal history for an entity slot."""
    with _store(ctx.obj["config"]) as store:
        eid = store.find_entity(entity)
        if eid is None:
            click.echo("Entity not found", err=True)
            sys.exit(1)
        h = store.get_slot_history(eid, key)
        if as_json:
            click.echo(json.dumps(h, ensure_ascii=False, indent=2))
        else:
            for entry in h:
                status = ("current" if entry["valid_to"] is None
                          else f"-> {entry['valid_to']}")
                click.echo(f"  {entry['valid_from']} | {entry['value']} | {status}")


# --- log ---

@main.command("log")
@click.argument("entity")
@click.argument("text")
@click.option("--date", default=None, help="Override date (YYYY-MM-DD).")
@click.pass_context
def add_log(ctx, entity, text, date):
    """Add a log entry to an entity."""
    with _store(ctx.obj["config"]) as store:
        eid = store.find_entity(entity)
        if eid is None:
            eid = store.resolve_entity(entity, "entity")
        store.add_log(eid, text, date=date)
        click.echo("OK")


# --- logs ---

@main.command("logs")
@click.argument("entity")
@click.option("--limit", type=int, default=None, help="Max entries.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def show_logs(ctx, entity, limit, as_json):
    """Show log entries for an entity."""
    with _store(ctx.obj["config"]) as store:
        eid = store.find_entity(entity)
        if eid is None:
            click.echo("Entity not found", err=True)
            sys.exit(1)
        logs = store.get_logs(eid, limit=limit)
        if as_json:
            click.echo(json.dumps(logs, ensure_ascii=False, indent=2))
        else:
            for log in logs:
                click.echo(f"  [{log['date']}] {log['text']}")


# --- generate ---

@main.command()
@click.argument("slug")
@click.option("--force", is_flag=True, help="Regenerate even if cache is fresh.")
@click.pass_context
def generate(ctx, slug, force):
    """Generate AI briefing for an agent."""
    from agent_recall.context_gen import generate_briefing
    config = ctx.obj["config"]
    result = generate_briefing(slug, config=config, force=force)
    if result:
        click.echo(f"Generated: {result}")
    else:
        click.echo(f"Skipped: no context or tier 0")


# --- refresh ---

@main.command()
@click.option("--force", is_flag=True, help="Regenerate all, ignoring cache.")
@click.pass_context
def refresh(ctx, force):
    """Refresh AI briefings for all agents."""
    from agent_recall.context_gen import generate_all
    config = ctx.obj["config"]
    results = generate_all(config=config, force=force)
    for slug, status in sorted(results.items()):
        icon = "\u2713" if status == "ok" else "\u2717" if "error" in status else "-"
        click.echo(f"  {icon} {slug}: {status}")


# --- rename-scope ---

@main.command("rename-scope")
@click.argument("old_scope")
@click.argument("new_scope")
@click.option("--dry-run", is_flag=True, help="Show what would change without modifying data.")
@click.pass_context
def rename_scope(ctx, old_scope, new_scope, dry_run):
    """Migrate all data from one scope to another."""
    with _store(ctx.obj["config"]) as store:
        if dry_run:
            counts = store.count_scope(old_scope)
            total = sum(counts.values())
            click.echo(f"Dry run: would rename {old_scope!r} -> {new_scope!r}: "
                        f"{counts['slots']} slots, {counts['observations']} observations, "
                        f"{counts['relations']} relations ({total} total)")
            return
        counts = store.rename_scope(old_scope, new_scope)
        total = sum(counts.values())
        click.echo(f"Renamed scope {old_scope!r} -> {new_scope!r}: "
                    f"{counts['slots']} slots, {counts['observations']} observations, "
                    f"{counts['relations']} relations ({total} total)")


# --- status ---

@main.command()
@click.pass_context
def status(ctx):
    """Show memory database status."""
    config = ctx.obj["config"]
    with _store(config) as store:
        entities = store.list_entities()
        by_type: dict[str, int] = {}
        for e in entities:
            by_type[e["type"]] = by_type.get(e["type"], 0) + 1
        click.echo(f"Database: {config.db_path}")
        click.echo(f"Entities: {len(entities)}")
        for t, count in sorted(by_type.items()):
            click.echo(f"  {t}: {count}")
