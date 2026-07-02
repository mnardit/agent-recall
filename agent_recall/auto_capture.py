"""PostToolUse auto-capture — 11 pattern extractors + NER + async write.

Inspired by memory-mcp mining.py (500+ lines of regex extractors).

Extractors:
  1. extract_imports        — Python import statements
  2. extract_facts          — "this project uses X"
  3. extract_commands       — shell commands
  4. extract_code_patterns  — def/class definitions
  5. extract_code_blocks    — markdown fenced code blocks
  6. extract_decisions      — "decided/chose/went with X because Y"
  7. extract_architecture   — "X handles Y" / "X communicates with Y via Z"
  8. extract_tech_stack     — known tech stack terms
  9. extract_explanations   — "because" / "in order to"
  10. extract_insights      — long paragraphs (100-800 chars) with key markers
  11. extract_config        — config facts (ports/timeouts/paths)

NER (optional, lazy load):
  DistilBERT NER (dslim/bert-base-NER) for Person/Organization/Location

Async write: daemon thread + queue.Queue (fire-and-forget).
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import threading
from dataclasses import dataclass, field

from agent_recall.privacy import redact

logger = logging.getLogger("agent_recall.auto_capture")


@dataclass
class CapturedObservation:
    """A pattern captured from tool output."""
    text: str
    pattern_type: str
    confidence: float = 0.5
    entity_type: str | None = None
    metadata: dict | None = None
    source_tool: str | None = None


# ---------------------------------------------------------------------------
# Known tech stack terms
# ---------------------------------------------------------------------------
KNOWN_TECH = {
    # Languages
    "python", "javascript", "typescript", "rust", "go", "golang", "java",
    "kotlin", "swift", "c++", "c#", "ruby", "php", "scala", "elixir",
    # Frontend
    "react", "vue", "angular", "svelte", "next.js", "nuxt", "remix",
    "tailwind", "bootstrap", "material-ui",
    # Backend
    "fastapi", "flask", "django", "express", "nestjs", "spring", "gin",
    "rails", "laravel", "graphql", "rest", "grpc",
    # Databases
    "postgresql", "mysql", "mongodb", "redis", "sqlite", "elasticsearch",
    "clickhouse", "dynamodb", "cassandra", "neo4j",
    # Infrastructure
    "docker", "kubernetes", "aws", "gcp", "azure", "terraform", "ansible",
    "nginx", "kafka", "rabbitmq", "celery",
    # Tools
    "git", "github", "gitlab", "jenkins", "webpack", "vite", "esbuild",
    "babel", "eslint", "prettier", "jest", "pytest",
}


# ---------------------------------------------------------------------------
# Extractor 1: Python imports
# ---------------------------------------------------------------------------
IMPORT_RE = re.compile(
    r'(?:^|\n)(?:from\s+(\S+)\s+import\s+(\S+)'
    r'|import\s+(\S+))',
    re.MULTILINE,
)

def extract_imports(text: str) -> list[CapturedObservation]:
    """Extract Python import statements."""
    results: list[CapturedObservation] = []
    for m in IMPORT_RE.finditer(text):
        full = m.group(0).strip()
        if len(full) > 8:
            results.append(CapturedObservation(
                text=full,
                pattern_type="import",
                confidence=0.9,
                entity_type="technology",
            ))
    return results


# ---------------------------------------------------------------------------
# Extractor 2: Facts
# ---------------------------------------------------------------------------
FACT_RE = re.compile(
    r'(?:this\s+project\s+(?:uses|is|has)|'
    r'we\s+(?:use|are|have)|'
    r'the\s+(?:project|codebase|system|app)\s+(?:uses|is|has))'
    r'\s+([^.!?\n]{10,200})',
    re.IGNORECASE,
)

def extract_facts(text: str) -> list[CapturedObservation]:
    """Extract factual statements about the project."""
    results: list[CapturedObservation] = []
    for m in FACT_RE.finditer(text):
        fact = m.group(0).strip()
        results.append(CapturedObservation(
            text=fact,
            pattern_type="fact",
            confidence=0.7,
            entity_type="fact",
        ))
    return results


# ---------------------------------------------------------------------------
# Extractor 3: Shell commands
# ---------------------------------------------------------------------------
CMD_RE = re.compile(
    r'(?:^|\n)\s*(?:\$\s*>?\s*|>\s*|`)\s*([\w./-]+(?:\s+[^\n]{2,200})?)',
    re.MULTILINE,
)

def extract_commands(text: str) -> list[CapturedObservation]:
    """Extract shell command patterns."""
    results: list[CapturedObservation] = []
    for m in CMD_RE.finditer(text):
        cmd = m.group(1).strip()
        if len(cmd) > 4 and not cmd.startswith("//"):
            results.append(CapturedObservation(
                text=cmd,
                pattern_type="command",
                confidence=0.6,
                entity_type="command",
            ))
    return results


# ---------------------------------------------------------------------------
# Extractor 4: Code patterns (def/class)
# ---------------------------------------------------------------------------
CODE_PATTERN_RE = re.compile(
    r'(?:^|\n)(?:async\s+)?(?:def|class)\s+(\w+)[\s\S]{0,200}?(?=\n(?:def|class|\S))',
    re.MULTILINE,
)

def extract_code_patterns(text: str) -> list[CapturedObservation]:
    """Extract function/class definitions."""
    results: list[CapturedObservation] = []
    for m in CODE_PATTERN_RE.finditer(text):
        results.append(CapturedObservation(
            text=m.group(0).strip(),
            pattern_type="code",
            confidence=0.85,
            entity_type="code",
        ))
    return results


# ---------------------------------------------------------------------------
# Extractor 5: Markdown code blocks
# ---------------------------------------------------------------------------
CODE_BLOCK_RE = re.compile(
    r'```(\w*)\n([\s\S]{10,800}?)```',
    re.MULTILINE,
)

def extract_code_blocks(text: str) -> list[CapturedObservation]:
    """Extract markdown fenced code blocks."""
    results: list[CapturedObservation] = []
    for m in CODE_BLOCK_RE.finditer(text):
        lang = m.group(1) or "unknown"
        code = m.group(2).strip()
        results.append(CapturedObservation(
            text=code[:500],
            pattern_type="code_block",
            confidence=0.7,
            entity_type="code",
            metadata={"language": lang},
        ))
    return results


# ---------------------------------------------------------------------------
# Extractor 6: Decisions
# ---------------------------------------------------------------------------
DECISION_RE = re.compile(
    r'(?:decided|chose|went\s+with|opted\s+for|picked|selected|'
    r'agreed\s+on|settled\s+on)\s+[\s\S]{10,300}?'
    r'(?:because|since|due\s+to|in\s+order\s+to|for\s+the\s+sake\s+of)'
    r'\s+[\s\S]{10,200}?(?:\.|$)',
    re.IGNORECASE,
)

def extract_decisions(text: str) -> list[CapturedObservation]:
    """Extract decision statements with rationale."""
    results: list[CapturedObservation] = []
    for m in DECISION_RE.finditer(text):
        decision = m.group(0).strip()
        results.append(CapturedObservation(
            text=decision,
            pattern_type="decision",
            confidence=0.75,
            entity_type="decision",
            metadata={"has_rationale": True},
        ))
    return results


# ---------------------------------------------------------------------------
# Extractor 7: Architecture statements
# ---------------------------------------------------------------------------
ARCH_RE = re.compile(
    r'(\w+(?:\s+\w+)?)\s+'
    r'(?:handles|manages|processes|communicates\s+with|depends\s+on|'
    r'calls|sends\s+to|receives\s+from|is\s+responsible\s+for)'
    r'\s+(\w+(?:\s+\w+){0,5})'
    r'(?:\s+(?:via|through|using|by)\s+(\w+(?:\s+\w+){0,5}))?',
    re.IGNORECASE,
)

def extract_architecture(text: str) -> list[CapturedObservation]:
    """Extract architectural relationship statements."""
    results: list[CapturedObservation] = []
    for m in ARCH_RE.finditer(text):
        results.append(CapturedObservation(
            text=m.group(0).strip(),
            pattern_type="architecture",
            confidence=0.65,
            entity_type="architecture",
            metadata={
                "from": m.group(1).strip(),
                "relation": m.group(2).strip().split()[0] if m.group(2) else "relates",
                "to": m.group(3).strip() if m.group(3) else None,
            },
        ))
    return results


# ---------------------------------------------------------------------------
# Extractor 8: Tech stack detection
# ---------------------------------------------------------------------------

def extract_tech_stack(text: str) -> list[CapturedObservation]:
    """Detect known technology mentions."""
    results: list[CapturedObservation] = []
    text_lower = text.lower()
    for tech in KNOWN_TECH:
        # Word boundary match
        if re.search(r'\b' + re.escape(tech) + r'\b', text_lower):
            results.append(CapturedObservation(
                text=tech,
                pattern_type="tech_stack",
                confidence=0.8,
                entity_type="technology",
                metadata={"name": tech},
            ))
    return results


# ---------------------------------------------------------------------------
# Extractor 9: Explanations
# ---------------------------------------------------------------------------
EXPLANATION_RE = re.compile(
    r'(?:because|since|in\s+order\s+to|the\s+reason\s+is|this\s+is\s+because|'
    r'as\s+a\s+result|therefore|consequently)'
    r'\s+[\s\S]{10,300}?(?:\.|$)',
    re.IGNORECASE,
)

def extract_explanations(text: str) -> list[CapturedObservation]:
    """Extract causal explanations."""
    results: list[CapturedObservation] = []
    for m in EXPLANATION_RE.finditer(text):
        expl = m.group(0).strip()
        results.append(CapturedObservation(
            text=expl,
            pattern_type="explanation",
            confidence=0.55,
            entity_type="insight",
        ))
    return results


# ---------------------------------------------------------------------------
# Extractor 10: Insights
# ---------------------------------------------------------------------------
INSIGHT_RE = re.compile(
    r'(?:interestingly|notably|importantly|key\s+(?:takeaway|insight|point)|'
    r'lesson\s+learned|best\s+practice|in\s+summary|tl;dr)'
    r'\s*:?\s*[\s\S]{20,300}?(?:\.|$)',
    re.IGNORECASE,
)

def extract_insights(text: str) -> list[CapturedObservation]:
    """Extract key insights and takeaways."""
    results: list[CapturedObservation] = []
    for m in INSIGHT_RE.finditer(text):
        insight = m.group(0).strip()
        results.append(CapturedObservation(
            text=insight,
            pattern_type="insight",
            confidence=0.6,
            entity_type="insight",
        ))
    return results


# ---------------------------------------------------------------------------
# Extractor 11: Config facts
# ---------------------------------------------------------------------------
CONFIG_RE = re.compile(
    r'(?:port|timeout|endpoint|host|connection|pool|limit|threshold|'
    r'rate|budget|cache|ttl|retry|batch)'
    r'\s*(?:is|set\s+to|at|of|=|:)\s*'
    r'[\w./:]{1,30}',
    re.IGNORECASE,
)

def extract_config(text: str) -> list[CapturedObservation]:
    """Extract configuration facts (ports, timeouts, etc.)."""
    results: list[CapturedObservation] = []
    for m in CONFIG_RE.finditer(text):
        cfg = m.group(0).strip()
        results.append(CapturedObservation(
            text=cfg,
            pattern_type="config",
            confidence=0.7,
            entity_type="config",
        ))
    return results


# ---------------------------------------------------------------------------
# All extractors
# ---------------------------------------------------------------------------
ALL_EXTRACTORS = [
    extract_imports,
    extract_facts,
    extract_commands,
    extract_code_patterns,
    extract_code_blocks,
    extract_decisions,
    extract_architecture,
    extract_tech_stack,
    extract_explanations,
    extract_insights,
    extract_config,
]


def extract_all_patterns(
    text: str,
    ner_enabled: bool = False,
) -> list[CapturedObservation]:
    """Run all extractors on text, deduplicate, return captured patterns."""
    all_captured: list[CapturedObservation] = []
    seen_hashes: set[str] = set()

    for extractor_fn in ALL_EXTRACTORS:
        try:
            captured = extractor_fn(text)
            for c in captured:
                h = hashlib.sha256(c.text.encode()).hexdigest()
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_captured.append(c)
        except Exception as e:
            logger.debug("%s failed: %s", extractor_fn.__name__, e)

    # NER (lazy load)
    if ner_enabled:
        try:
            ner_results = extract_entities_ner(text)
            for c in ner_results:
                h = hashlib.sha256(c.text.encode()).hexdigest()
                if h not in seen_hashes:
                    seen_hashes.add(h)
                    all_captured.append(c)
        except Exception as e:
            logger.debug("NER extraction failed: %s", e)

    return all_captured


def extract_entities_ner(
    text: str,
    min_confidence: float = 0.7,
) -> list[CapturedObservation]:
    """Extract named entities using DistilBERT NER (lazy load).

    Requires: pip install transformers torch
    """
    try:
        from transformers import pipeline
    except ImportError:
        logger.debug("transformers not installed — skipping NER")
        return []

    try:
        ner = pipeline(
            "ner", model="dslim/bert-base-NER",
            aggregation_strategy="simple",
        )
        entities = ner(text[:2000])  # Truncate for performance
    except Exception as e:
        logger.debug("NER pipeline failed: %s", e)
        return []

    results: list[CapturedObservation] = []
    for ent in entities:
        if ent["score"] >= min_confidence:
            entity_type_map = {
                "PER": "person",
                "ORG": "organization",
                "LOC": "location",
                "MISC": "concept",
            }
            results.append(CapturedObservation(
                text=ent["word"],
                pattern_type="named_entity",
                confidence=ent["score"],
                entity_type=entity_type_map.get(ent["entity_group"], "concept"),
                metadata={"ner_label": ent["entity_group"]},
            ))
    return results


# ---------------------------------------------------------------------------
# Async write queue
# ---------------------------------------------------------------------------
_async_queue: queue.Queue = queue.Queue()
_async_thread: threading.Thread | None = None
_async_lock = threading.Lock()


def _async_worker(store_db_path: str) -> None:
    """Daemon thread that writes captured patterns to the store."""
    from agent_recall.store import MemoryStore
    try:
        store = MemoryStore(store_db_path)
        while True:
            try:
                item = _async_queue.get(timeout=5)
                if item is None:  # Sentinel to stop
                    break
                captured_list, scope, source_tool = item

                for cap in captured_list:
                    if cap.confidence >= 0.5:
                        # Redact secrets
                        safe_text = redact(cap.text)
                        # Create or reuse entity
                        entity_name = cap.entity_type or "knowledge"
                        eid = store.resolve_entity(entity_name, "auto_captured")
                        oid = store.add_observation(eid, safe_text, scope=scope)
                        # Store pattern
                        store.upsert_pattern(
                            safe_text, cap.pattern_type,
                            confidence=cap.confidence,
                            source_entity_type=cap.entity_type,
                            metadata=cap.metadata,
                        )
                        # Auto-assign privacy
                        store.set_privacy(oid, "public")

                _async_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.debug("Async write failed: %s", e)
    finally:
        try:
            store.close()
        except Exception:
            pass


def enqueue_auto_capture(
    store_db_path: str,
    captured: list[CapturedObservation],
    scope: str,
    source_tool: str,
) -> None:
    """Enqueue captured patterns for async writing.

    Starts the daemon thread on first call.
    """
    global _async_thread
    with _async_lock:
        if _async_thread is None or not _async_thread.is_alive():
            _async_thread = threading.Thread(
                target=_async_worker,
                args=(store_db_path,),
                daemon=True,
                name="agent-recall-auto-capture",
            )
            _async_thread.start()

    _async_queue.put((captured, scope, source_tool))


import re as _re

_NOISE_PATTERNS = [
    r"^Path: [A-Z]:\\\\",
    r"^Session: [a-f0-9-]+",
    r"^Date: \d{4}-\d{2}-\d{2}\nMessages:",
    r"^Spatial location:",
    r"DO NOT respond to these",
    r"don't depend on this",
    r"^Hash: [a-f0-9]+$",
    r"^Source: \w+$",
    r"Duration: \d+min",
]
_NOISE_RE = [_re.compile(p) for p in _NOISE_PATTERNS]


def _is_noise(text):
    """Check if text looks like auto-generated metadata rather than real knowledge."""
    if len(text) < 20:
        return True
    for pat in _NOISE_RE:
        if pat.search(text):
            return True
    return False


class AutoCaptureEngine:
    """Main engine for automatic pattern capture from tool output.

    Usage::

        engine = AutoCaptureEngine(store)
        count = engine.capture_from_tool_output("Read", {}, file_content)
    """

    def __init__(self, store, config=None) -> None:  # AutoCaptureConfig | None
        self._store = store
        self._config = config
        self._min_confidence = getattr(config, "min_confidence", 0.6) if config else 0.6
        self._ner_enabled = getattr(config, "ner_enabled", False) if config else False

    def capture_from_tool_output(
        self,
        tool_name: str,
        tool_input: dict,
        tool_output: str,
    ) -> int:
        """Extract patterns from tool output and store them.

        Args:
            tool_name: Name of the tool that produced the output.
            tool_input: Tool's input parameters.
            tool_output: Tool's output text.

        Returns:
            Number of observations created.
        """
        if not tool_output or not tool_output.strip():
            return 0

        # Skip if output is too short or too long
        if len(tool_output) < 10 or len(tool_output) > 50000:
            return 0

        # Skip noise: session metadata, skill paths, system prompt fragments
        if _is_noise(tool_output):
            return 0

        # Privacy check
        if _may_contain_secrets(tool_output):
            safe_output = redact(tool_output)
        else:
            safe_output = tool_output

        captured = extract_all_patterns(safe_output, ner_enabled=self._ner_enabled)

        # Filter by minimum confidence
        captured = [c for c in captured if c.confidence >= self._min_confidence]

        if not captured:
            return 0

        # Store
        count = 0
        scope = "global"  # Can be overridden by config
        for cap in captured:
            try:
                safe_text = redact(cap.text)
                entity_name = cap.entity_type or "knowledge"
                eid = self._store.resolve_entity(entity_name, "auto_captured")
                oid = self._store.add_observation(eid, safe_text, scope=scope)
                self._store.upsert_pattern(
                    safe_text, cap.pattern_type,
                    confidence=cap.confidence,
                    source_entity_type=cap.entity_type,
                    metadata=cap.metadata,
                )
                self._store.set_privacy(oid, "public")
                count += 1
            except Exception as e:
                logger.debug("Failed to store captured pattern: %s", e)

        return count

    def run_mining(
        self,
        hours: int = 24,
        scope: str | None = None,
    ) -> dict:
        """Batch-mine recent observations for patterns (offline mode).

        Returns {patterns_found, new_memories, ...}.
        """
        from datetime import datetime, timedelta, timezone
        since = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()

        scope_filter = "AND scope = ?" if scope else ""
        params = [since]
        if scope:
            params.append(scope)

        rows = self._store._conn.execute(
            f"SELECT text FROM observations "
            f"WHERE created_at >= ? AND archived_at IS NULL "
            f"{scope_filter}",
            params,
        ).fetchall()

        patterns_found = 0
        for row in rows:
            captured = extract_all_patterns(row["text"])
            for cap in captured:
                try:
                    self._store.upsert_pattern(
                        cap.text, cap.pattern_type,
                        confidence=cap.confidence,
                        source_entity_type=cap.entity_type,
                        metadata=cap.metadata,
                    )
                    patterns_found += 1
                except Exception:
                    pass

        return {
            "observations_scanned": len(rows),
            "patterns_found": patterns_found,
        }


def _may_contain_secrets(text: str) -> bool:
    """Quick check if text might contain credentials before redacting."""
    danger_words = [
        "password", "secret", "token", "api_key", "apikey",
        "ghp_", "sk-", "Bearer ", "Authorization:",
    ]
    text_lower = text.lower()
    return any(dw in text_lower or dw in text for dw in danger_words)
