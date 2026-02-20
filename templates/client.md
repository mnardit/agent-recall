You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble, no changelogs, no "key updates" summaries. This is a fresh generation — do NOT reference previous versions. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — manages a client project.

INSTRUCTIONS (read these BEFORE processing the raw data):

Generate a briefing with these sections:

## Key People
Include people with detailed entries. For each: name, role, contact, key facts.
From "Other team members", include ONLY those listed in CLAUDE.md People section. Skip unrelated team members.
Use role descriptions from "## Role Descriptions" section (e.g. "lead developer", "backup developer") over generic slot data.

## Current Tasks
Prioritize: urgent > in progress > can wait.

## Constraints & Rules
Copy from "## Agent Constraints" section in raw data. If that section is absent, write "None specified." Do NOT invent constraints.

## Context
Recent events, decisions, agreements.

## Relations
Key dependencies between people and projects.

RULES:
- Output ONLY information from the raw data below. Do NOT hallucinate.
- CLAUDE.md (in "## Project Files" at the end) has operational rules — extract constraints and role descriptions from it.
- Exclude: raw slot data, entity IDs, scope metadata, completed tasks.
- Language: match the raw data.

---

Raw data from knowledge base:
{raw_context}
