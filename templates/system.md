You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble, no changelogs, no "key updates" summaries. This is a fresh generation — do NOT reference previous versions. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — system utility/service agent.

INSTRUCTIONS (read before processing raw data):

Generate a minimal, technical briefing. System agents need precise operational details, not lengthy context.

## Role
What this agent does — one paragraph.

## People
Only people who directly interact with or maintain this service.

## Context
Technical details: service name, how to restart, tests, DB, APIs, key config. Extract from CLAUDE.md in Project Files.

RULES:
- Keep it short and technical.
- Output only information from the raw data.
- Language: match the raw data.

---

Raw data:
{raw_context}
