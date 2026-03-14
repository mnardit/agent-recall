"""Shared helpers for context assembly functions.

Used by context.py (regular agents), and context_gen/assembly.py
(orchestrator and topic agents) to avoid duplicating formatting logic.
"""

MAX_OBSERVATION_LENGTH = 2000


def format_observation(text: str) -> str:
    """Truncate, XML-escape, and wrap an observation in XML tags.

    >>> format_observation("short note")
    '<observation>short note</observation>'
    """
    safe = text[:MAX_OBSERVATION_LENGTH]
    safe = safe.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<observation>{safe}</observation>"


def format_slots(slots: dict[str, str]) -> str:
    """Format a slots dict as a comma-separated key: value string.

    >>> format_slots({"role": "Engineer", "email": "a@b.com"})
    'role: Engineer, email: a@b.com'
    """
    return ", ".join(f"{k}: {v}" for k, v in slots.items())


def format_entity_header(name: str, slots: dict[str, str] | None = None,
                         entity_type: str | None = None,
                         bold: bool = True) -> str:
    """Format an entity name with optional slots/type as a markdown line.

    Args:
        name: Entity name.
        slots: Slot key-value pairs (formatted inline).
        entity_type: If provided, shown before slots in parentheses.
        bold: Wrap name in **bold**.

    Examples:
        >>> format_entity_header("Alice", {"role": "Dev"})
        '**Alice** (role: Dev)'
        >>> format_entity_header("Bob", {"role": "PM"}, entity_type="person")
        '**Bob** (person, role: PM)'
        >>> format_entity_header("Carol", bold=False)
        'Carol'
    """
    label = f"**{name}**" if bold else name
    parts: list[str] = []
    if entity_type:
        parts.append(entity_type)
    if slots:
        parts.append(format_slots(slots))
    if parts:
        label += f" ({', '.join(parts)})"
    return label


def format_relation(from_name: str, rel: dict) -> str:
    """Format a relation as a markdown line.

    Args:
        from_name: Source entity name.
        rel: Relation dict with 'type', 'to_name', and optional 'context'.

    Returns:
        Formatted string like ``Alice —[works_at]→ Acme (context)``.
    """
    line = f"- {from_name} \u2014[{rel['type']}]\u2192 {rel['to_name']}"
    if rel.get("context"):
        line += f" ({rel['context']})"
    return line


def format_log_entry(entity_name: str, log_entry: dict) -> tuple[str, str]:
    """Format a log entry, returning (date, formatted_line) for sorting.

    Args:
        entity_name: Name of the entity the log belongs to.
        log_entry: Dict with 'date' and 'text' keys.

    Returns:
        Tuple of (date_str, formatted_line).
    """
    date = log_entry.get("date", "")
    text = log_entry.get("text", "")
    return (date, f"- [{date}] {entity_name}: {text}")


def apply_budget(result: str, budget: int) -> str:
    """Hard-truncate result to budget length.

    >>> apply_budget("hello world", 5)
    'hello'
    >>> apply_budget("hi", 100)
    'hi'
    """
    return result[:budget] if len(result) > budget else result
