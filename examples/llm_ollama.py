"""Use Ollama (local) as the LLM for briefing generation."""
import requests

from agent_recall import generate_briefing, LLMResult


def ollama_caller(prompt: str, model: str, timeout: int) -> LLMResult:
    resp = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": model or "llama3.1", "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return LLMResult(
        text=data["response"],
        input_tokens=data.get("prompt_eval_count"),
        output_tokens=data.get("eval_count"),
    )


if __name__ == "__main__":
    generate_briefing("my-agent", llm_caller=ollama_caller, force=True)
