You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — system utility/service agent.

Raw data:
{raw_context}

Create a minimal briefing:

## Role
What this agent does in the system.

## People
Only people who interact with this service.

## Context
System-level context this agent needs.

Keep it very short — system agents need minimal people context.
Language: match the raw data language.