"""File discovery and loading for context generation."""
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# Common project instruction files across editors, in discovery order.
DISCOVERABLE_FILES = [
    "CLAUDE.md",              # Claude Code
    ".claude/CLAUDE.md",      # Claude Code (nested)
    ".cursorrules",           # Cursor
    ".cursor/rules",          # Cursor (nested)
    ".windsurfrules",         # Windsurf
    "README.md",              # Universal
]


def _discover_project_files(project_dir: Path | None = None) -> list[Path]:
    """Auto-discover project instruction files in a directory.

    Scans for common editor-specific and general project files.
    Returns list of existing file paths, in priority order.
    """
    base = project_dir or Path.cwd()
    found = []
    for name in DISCOVERABLE_FILES:
        path = base / name
        if path.is_file():
            found.append(path)
    return found


def _extract_claude_md_sections(paths: list[Path]) -> dict[str, str]:
    """Extract key sections from CLAUDE.md files for prominent placement in raw data.

    Returns dict with optional keys: 'constraints', 'people_roles'.
    """
    result: dict[str, str] = {}
    for path in paths:
        if path.name != "CLAUDE.md":
            continue
        resolved = path.resolve()
        if not resolved.is_file():
            continue
        try:
            content = resolved.read_text(encoding="utf-8")
        except OSError:
            continue

        for header in ("Constraints", "Rules"):
            pattern = rf"^## {header}\s*\n(.*?)(?=\n## |\Z)"
            match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
            if match:
                text = match.group(1).strip()
                if text:
                    result["constraints"] = result.get("constraints", "") + text + "\n"

        # Extract ## People section for role descriptions
        people_match = re.search(
            r"^## People\s*\n(.*?)(?=\n## |\Z)", content, re.MULTILINE | re.DOTALL
        )
        if people_match:
            result["people_roles"] = people_match.group(1).strip()

        break  # Only process first CLAUDE.md
    return result


def _load_context_files(paths: list[Path], budget: int,
                        allowed_bases: list[Path] | None = None) -> str:
    """Read files and concatenate, truncating to budget.

    Missing, unreadable, or non-regular files are skipped with a log warning.

    Args:
        paths: List of file paths to read.
        budget: Maximum total characters to include.
        allowed_bases: Optional list of allowed base directories. Files outside
            these directories are skipped. If None, no restriction is applied.
    """
    sections: list[str] = []
    used = 0
    for path in paths:
        resolved = path.resolve()
        if not resolved.is_file():
            log.warning("Context file is not a regular file, skipping: %s", path)
            continue
        # Validate path if allowed_bases specified
        if allowed_bases:
            if not any(resolved.is_relative_to(base.resolve()) for base in allowed_bases):
                log.warning("Context file outside allowed directories, skipping: %s", path)
                continue
        try:
            content = resolved.read_text(encoding="utf-8")
        except OSError:
            log.warning("Cannot read context file: %s", path)
            continue
        remaining = budget - used
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = content[:remaining] + "\n... (truncated)"
        sections.append(f"### {path.name}\n{content}")
        used += len(content)
    return "\n\n".join(sections)
