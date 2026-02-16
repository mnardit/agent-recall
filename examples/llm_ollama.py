"""Use Ollama (local) as the LLM for briefing generation."""
import requests

from agent_recall import generate_briefing


def ollama_caller(prompt: str, model: str, timeout: int) -> str:
    resp = requests.post(
        "http://localhost:11434/api/generate",
        json={"model": "llama3.1", "prompt": prompt, "stream": False},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["response"]


if __name__ == "__main__":
    generate_briefing("my-agent", llm_caller=ollama_caller, force=True)
