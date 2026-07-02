"""Privacy protection: secret detection, redaction, and privacy-level tagging.

Detects:
  - API keys: sk-..., ghp_..., AKIA..., rk-...
  - Connection strings: mongodb+srv://..., postgres://...
  - Bearer tokens, email addresses, IP addresses
  - <private>...</private> markup tags

Inspired by: memory-mcp redaction.py + ArcRift PII scrubbing.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Detection patterns: (regex, replacement_label)
# ---------------------------------------------------------------------------
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    # OpenAI / Anthropic keys
    (r'sk-(?:proj-)?[A-Za-z0-9]{32,}', '[OPENAI_KEY_REDACTED]'),
    (r'sk-ant-[A-Za-z0-9]{32,}', '[ANTHROPIC_KEY_REDACTED]'),
    # GitHub PAT
    (r'ghp_[A-Za-z0-9]{36,}', '[GITHUB_PAT_REDACTED]'),
    (r'github_pat_[A-Za-z0-9_]{40,}', '[GITHUB_PAT_REDACTED]'),
    # AWS keys
    (r'AKIA[0-9A-Z]{16}', '[AWS_KEY_REDACTED]'),
    (r'ASIA[0-9A-Z]{16}', '[AWS_TEMP_KEY_REDACTED]'),
    # Generic credential assignment
    (
        r'(?:password|secret|token|api[_-]?key|auth[_-]?token)\s*[:=]\s*[\'\"][\w\-./+]{8,}[\'\"]',
        '[CREDENTIAL_REDACTED]',
    ),
    # Connection strings with embedded credentials
    (r'://[^:]+:[^@]+@', '://[USER]:[REDACTED]@'),
    # Bearer tokens in headers
    (r'Bearer\s+[A-Za-z0-9_\-\.]{20,}', 'Bearer [REDACTED]'),
    # Private markup tags
    (r'<private>.*?</private>', '[PRIVATE]',),
    # Email addresses
    (r'[\w\.-]+@[\w\.-]+\.\w{2,}', '[EMAIL]'),
    # Internal IPs (keep public IPs — only redact 10.x, 172.16-31.x, 192.168.x)
    (
        r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
        r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}'
        r'|192\.168\.\d{1,3}\.\d{1,3})\b',
        '[INTERNAL_IP]',
    ),
]

# Compile once
_COMPILED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE | re.DOTALL), label)
    for p, label in SENSITIVE_PATTERNS
]

PRIVACY_LEVELS = ("public", "private", "sensitive", "redacted")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_secrets(text: str) -> list[tuple[str, str, int, int]]:
    """Scan text for secrets. Returns [(match_text, replacement_label, start, end), ...]."""
    hits: list[tuple[str, str, int, int]] = []
    for pattern, label in _COMPILED_PATTERNS:
        for m in pattern.finditer(text):
            hits.append((m.group(), label, m.start(), m.end()))
    # Remove nested hits (shorter match fully inside longer one)
    hits.sort(key=lambda x: (x[2], -(x[3] - x[2])))
    filtered: list[tuple[str, str, int, int]] = []
    last_end = -1
    for hit in hits:
        if hit[2] >= last_end:
            filtered.append(hit)
            last_end = hit[3]
    return filtered


def redact(text: str) -> str:
    """Replace all detected secrets with [REDACTED] labels.

    Returns the sanitized string.
    """
    hits = detect_secrets(text)
    if not hits:
        return text
    # Replace from end to start to preserve offsets
    result = text
    for _, label, start, end in reversed(hits):
        result = result[:start] + label + result[end:]
    return result


def has_private_tag(text: str) -> bool:
    """Check if text contains <private>...</private> markup."""
    return bool(re.search(r'<private>', text, re.IGNORECASE))


def assign_privacy_level(
    text: str,
    explicit_tag: str | None = None,
) -> str:
    """Auto-assign privacy level based on content.

    Priority: explicit_tag > secret detection > private tags > default public.

    Returns one of: 'public', 'private', 'sensitive', 'redacted'.
    """
    if explicit_tag and explicit_tag in PRIVACY_LEVELS:
        return explicit_tag

    hits = detect_secrets(text)
    if hits:
        return "sensitive"

    if has_private_tag(text):
        return "private"

    return "public"


def filter_by_privacy(
    observations: list[dict],
    agent_level: str = "personal",
) -> list[dict]:
    """Filter observation list by agent's privacy clearance.

    Clearance levels:
        system   — sees all (public, private, sensitive)
        personal — sees public + private (default)
        default  — sees public only

    Each observation dict should have a 'privacy' key (defaults to 'public'
    if missing).
    """
    visible = _visible_levels(agent_level)
    return [
        obs for obs in observations
        if obs.get("privacy", "public") in visible
    ]


def _visible_levels(agent_level: str) -> set[str]:
    """Map agent level to visible privacy levels."""
    mapping = {
        "system": {"public", "private", "sensitive"},
        "orchestrator": {"public", "private", "sensitive"},
        "personal": {"public", "private"},
        "default": {"public"},
    }
    return mapping.get(agent_level, {"public"})
