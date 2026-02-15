"""Episodic memory — session summaries."""
from datetime import datetime, timezone, timedelta
from pathlib import Path


def save_episode(episodes_dir: Path, project: str, session_id: str,
                 summary: str, decisions: list[str], open_items: list[str],
                 trigger: str = "compact") -> Path:
    episodes_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    filename = f"{now.strftime('%Y-%m-%d_%H%M')}_{session_id[:8]}.md"
    path = episodes_dir / filename
    lines = [
        "---", f"date: {now.strftime('%Y-%m-%d')}", f"project: {project}",
        f"session: {session_id}", f"trigger: {trigger}", "---", "",
        "## Summary", summary, "",
    ]
    if decisions:
        lines += ["## Decisions"] + [f"- {d}" for d in decisions] + [""]
    if open_items:
        lines += ["## Open"] + [f"- {i}" for i in open_items] + [""]
    path.write_text("\n".join(lines))
    return path


def load_recent_episodes(episodes_dir: Path, days: int = 3,
                         project: str | None = None) -> list[dict]:
    if not episodes_dir.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    episodes = []
    for f in sorted(episodes_dir.glob("*.md")):
        try:
            file_date = datetime.strptime(f.name[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        content = f.read_text()
        if project and f"project: {project}" not in content:
            continue
        episodes.append({"path": str(f), "date": f.name[:10], "content": content})
    return episodes
