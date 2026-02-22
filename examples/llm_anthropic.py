"""Use the Anthropic SDK as the LLM for briefing generation.

NOTE: agent-recall has a built-in 'api' backend that does this automatically.
Just set `backend: api` in your memory.yaml briefing section.
This example shows how to customize the Anthropic call further.
"""
import anthropic

from agent_recall import generate_briefing, LLMResult


def anthropic_caller(prompt: str, model: str, timeout: int) -> LLMResult:
    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
    resp = client.messages.create(
        model=model or "claude-sonnet-4-5-20250929",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
        timeout=timeout,
    )
    return LLMResult(
        text=resp.content[0].text,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )


if __name__ == "__main__":
    generate_briefing("my-agent", llm_caller=anthropic_caller, force=True)
