"""LLM invocation backends for AI briefing generation."""
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Callable

from agent_recall.config import MemoryConfig

log = logging.getLogger(__name__)

# Defaults (overridable via config.briefing)
DEFAULT_MODEL = "opus"
DEFAULT_TIMEOUT = 300

# Type for pluggable LLM invocation: (prompt, model, timeout) -> str | None | LLMResult
LLMCaller = Callable[[str, str, int], "str | None | LLMResult"]


@dataclass
class LLMResult:
    """Structured result from LLM invocation with optional token counts."""
    text: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None


# Model name mapping: short names used in config -> full API model IDs.
# Users write `model: opus` in YAML; the API backend resolves it to the full ID.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _validate_model(model: str) -> bool:
    """Check model name contains only safe characters (alphanumeric, hyphens, dots)."""
    return bool(model) and all(c.isalnum() or c in "-_." for c in model)


def _resolve_model(model: str) -> str:
    """Resolve short model alias to full API model ID."""
    return MODEL_ALIASES.get(model, model)


def _cli_llm_caller(prompt: str, model: str = DEFAULT_MODEL,
                     timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """LLM caller via Claude Code CLI (`claude -p`).

    Free on Max/Team subscription. Creates a session file per invocation.
    """
    if not _validate_model(model):
        log.error("Invalid model name rejected: %r", model)
        return None
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model],
            input=prompt,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            log.warning("LLM stderr: %s", result.stderr[:200])
        return None
    except subprocess.TimeoutExpired:
        log.warning("LLM call timed out after %ds", timeout)
        return None
    except FileNotFoundError:
        log.error(
            "claude CLI not found. Install Claude Code or pass a custom "
            "llm_caller (see examples/llm_openai.py)"
        )
        return None


def _api_llm_caller(prompt: str, model: str = DEFAULT_MODEL,
                     timeout: int = DEFAULT_TIMEOUT) -> "str | LLMResult | None":
    """LLM caller via Anthropic SDK. Clean, no session files.

    Requires: pip install 'agent-recall[api]' and ANTHROPIC_API_KEY env var.
    """
    try:
        import anthropic
    except ImportError:
        log.error(
            "Anthropic SDK not installed. Install with: "
            "pip install 'agent-recall[api]'"
        )
        return None

    api_model = _resolve_model(model)
    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=api_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout,
        )
        text = resp.content[0].text if resp.content else None
        return LLMResult(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
    except Exception as e:
        log.error("Anthropic API call failed: %s", e)
        return None


def _get_default_caller(config: MemoryConfig | None = None) -> LLMCaller:
    """Return the appropriate LLM caller based on config.

    Config key: briefing.backend -- "cli" (default) or "api".
    """
    if config:
        backend = config.briefing.get("backend", "cli")
    else:
        backend = "cli"

    if backend == "api":
        return _api_llm_caller
    return _cli_llm_caller
