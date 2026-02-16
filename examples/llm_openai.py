"""Use OpenAI as the LLM for briefing generation."""
from openai import OpenAI

from agent_recall import generate_briefing, LLMResult


def openai_caller(prompt: str, model: str, timeout: int) -> LLMResult:
    client = OpenAI()  # uses OPENAI_API_KEY env var
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        timeout=timeout,
    )
    return LLMResult(
        text=resp.choices[0].message.content,
        input_tokens=resp.usage.prompt_tokens,
        output_tokens=resp.usage.completion_tokens,
    )


if __name__ == "__main__":
    generate_briefing("my-agent", llm_caller=openai_caller, force=True)
