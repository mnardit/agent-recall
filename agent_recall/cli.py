"""CLI for agent-recall — manage agent memory from the terminal."""
import json
import os
import sys
from pathlib import Path

import click

from agent_recall import __version__
from agent_recall.config import MemoryConfig, load_config, DEFAULT_DB_PATH
from agent_recall.store import MemoryStore


def _store(config: MemoryConfig) -> MemoryStore:
    return MemoryStore(config.db_path)


@click.group()
@click.version_option(version=__version__, prog_name="agent-recall")
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
    click.echo(f"\u2713 Database initialized at {db_path}")
    click.echo("")
    click.echo("Next steps:")
    click.echo("  1. Configure your MCP client to use agent-recall:")
    click.echo("")
    click.echo("     For Claude Code, add to .mcp.json:")
    click.echo('       {"mcpServers": {"memory": {"command": "python3", '
               '"args": ["-m", "agent_recall.mcp_server"]}}}')
    click.echo("")
    click.echo("  2. Set your agent slug:")
    click.echo("       export AGENT_RECALL_SLUG=my-agent")
    click.echo("")
    click.echo("  3. Generate your first briefing:")
    click.echo("       agent-recall generate my-agent")
    click.echo("")
    click.echo("  4. (Optional) Install hooks for auto-briefing on session start.")
    click.echo("     See: https://github.com/mnardit/agent-recall#setup")
    click.echo("")
    click.echo("Full documentation: https://github.com/mnardit/agent-recall")


# --- set ---

@main.command("set")
@click.argument("entity")
@click.argument("key")
@click.argument("value")
@click.option("--type", "entity_type", default=None,
              help="Entity type (required for new entities, e.g. --type person).")
@click.option("--scope", default="global", help="Scope for the slot.")
@click.pass_context
def set_slot(ctx, entity, key, value, entity_type, scope):
    """Set a slot value on an entity.

    For existing entities, --type is optional. For new entities, --type is required.
    """
    with _store(ctx.obj["config"]) as store:
        existing = store.find_entity(entity)
        if existing is not None:
            eid = existing
        elif entity_type:
            eid = store.resolve_entity(entity, entity_type)
        else:
            click.echo(f"Entity '{entity}' not found. Use --type to create it.", err=True)
            sys.exit(1)
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
            click.echo(f"Entity not found: '{entity}'", err=True)
            sys.exit(1)
        val = store.get_slot(eid, key)
        if val is None:
            click.echo(f"Slot '{key}' not found on entity '{entity}'", err=True)
            sys.exit(1)
        click.echo(val)


# --- entity ---

@main.command()
@click.argument("name")
@click.option("--scope", multiple=True,
              help="Scope chain for slot resolution (repeatable, e.g. --scope global --scope acme).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def entity(ctx, name, scope, as_json):
    """Show entity details."""
    with _store(ctx.obj["config"]) as store:
        eid = store.find_entity(name)
        if eid is None:
            click.echo(f"Entity not found: '{name}'", err=True)
            sys.exit(1)
        e = store.get_entity(eid)
        scope_chain = list(scope) if scope else None
        slots = store.get_slots(eid, scope_chain=scope_chain)
        obs = store.get_observations(eid)
        if as_json:
            click.echo(json.dumps({"id": e["id"], "name": e["name"],
                                   "type": e["type"], "slots": slots,
                                   "observations": obs},
                                  ensure_ascii=False, indent=2))
        else:
            click.echo(f"{e['name']} ({e['type']})")
            for k, v in slots.items():
                click.echo(f"  {k}: {v}")
            if obs:
                click.echo("  observations:")
                for o in obs:
                    click.echo(f"    - {o['text']}")


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
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def search(ctx, query, as_json):
    """Search entities by name, slot, or observation."""
    with _store(ctx.obj["config"]) as store:
        results = store.search(query)
        if as_json:
            click.echo(json.dumps(results, ensure_ascii=False, indent=2))
        else:
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
            click.echo(f"Entity not found: '{entity}'", err=True)
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
@click.option("--author", default="agent", help="Author of the log entry.")
@click.pass_context
def add_log(ctx, entity, text, date, author):
    """Add a log entry to an entity."""
    with _store(ctx.obj["config"]) as store:
        eid = store.find_entity(entity)
        if eid is None:
            eid = store.resolve_entity(entity, "entity")
        store.add_log(eid, text, date=date, author=author)
        click.echo(f"Logged to {entity}: {text[:80]}")


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
            click.echo(f"Entity not found: '{entity}'", err=True)
            sys.exit(1)
        logs = store.get_logs(eid, limit=limit)
        if as_json:
            click.echo(json.dumps(logs, ensure_ascii=False, indent=2))
        else:
            for log in logs:
                click.echo(f"  [{log['date']}] {log['text']}")


# --- templates ---

@main.command()
@click.pass_context
def templates(ctx):
    """List available briefing template types."""
    from agent_recall.context_gen.templates import (
        BUILTIN_TEMPLATES, list_templates,
    )
    config = ctx.obj["config"]
    templates_dir = config.templates_dir
    names = list_templates(templates_dir)
    for name in names:
        if templates_dir and (templates_dir / f"{name}.md").is_file():
            source = "file"
        elif name in BUILTIN_TEMPLATES:
            source = "builtin"
        else:
            source = "file"
        click.echo(f"  {name} ({source})")


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
        try:
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
        except ValueError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)


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


# --- observe ---

@main.command()
@click.argument("entity_name")
@click.argument("text")
@click.option("--scope", default=None, help="Scope for the observation.")
@click.pass_context
def observe(ctx, entity_name, text, scope):
    """Add an observation to an entity."""
    with _store(ctx.obj["config"]) as store:
        entity_id = store.find_entity(entity_name)
        if entity_id is None:
            click.echo(f"Entity not found: {entity_name}", err=True)
            raise SystemExit(1)
        scope = scope or os.environ.get("AGENT_RECALL_SLUG") or "global"
        obs_id = store.add_observation(entity_id, text, scope=scope)
        click.echo(f"Added observation #{obs_id} to {entity_name}")


# --- delete ---

@main.command()
@click.argument("entity_name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@click.pass_context
def delete(ctx, entity_name, yes):
    """Delete an entity from the knowledge graph."""
    with _store(ctx.obj["config"]) as store:
        entity_id = store.find_entity(entity_name)
        if entity_id is None:
            click.echo(f"Entity not found: {entity_name}", err=True)
            raise SystemExit(1)
        if not yes:
            click.confirm(f"Delete entity '{entity_name}' and all its data?", abort=True)
        store.delete_entity(entity_id)
        click.echo(f"Deleted: {entity_name}")
