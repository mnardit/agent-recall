You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble, no changelogs, no "key updates" summaries. This is a fresh generation — do NOT reference previous versions. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — personal/side project.

INSTRUCTIONS (read before processing raw data):

Generate a concise briefing. Personal projects need less context than client work — keep it focused.

## People
Who's involved and their roles. Include all people from the data.

## Tasks
Current tasks and priorities. If no tasks, say so.

## Constraints & Rules
Copy from "## Agent Constraints" section if present. If absent, omit this section entirely.

## Context
What the agent needs to know. Include project-specific details from CLAUDE.md in Project Files.

RULES:
- Output only information from the raw data.
- Language: match the raw data.

---

Raw data:
{raw_context}
