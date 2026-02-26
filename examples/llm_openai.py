"""Use OpenAI as the LLM for briefing generation.

Uses the Responses API (recommended). Chat Completions also works —
see comments below for the legacy approach.
"""
from openai import OpenAI

from agent_recall import generate_briefing, LLMResult


def openai_caller(prompt: str, model: str, timeout: int) -> LLMResult:
    client = OpenAI()  # uses OPENAI_API_KEY env var
    response = client.responses.create(
        model=model or "gpt-4o",
        input=prompt,
    )
    return LLMResult(
        text=response.output_text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


# Legacy Chat Completions approach (also works):
#
# def openai_caller(prompt: str, model: str, timeout: int) -> LLMResult:
#     client = OpenAI()
#     resp = client.chat.completions.create(
#         model=model or "gpt-4o",
#         messages=[{"role": "user", "content": prompt}],
#         timeout=timeout,
#     )
#     return LLMResult(
#         text=resp.choices[0].message.content,
#         input_tokens=resp.usage.prompt_tokens,
#         output_tokens=resp.usage.completion_tokens,
#     )


if __name__ == "__main__":
    generate_briefing("my-agent", llm_caller=openai_caller, force=True)
