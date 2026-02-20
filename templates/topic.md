You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble, no changelogs, no "key updates" summaries. This is a fresh generation — do NOT reference previous versions. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — focused topic/sub-session within a larger project.
Topics are temporary workstreams that need sharp focus.

INSTRUCTIONS (read before processing raw data):

Generate a focused briefing — only what's directly relevant to this topic.

## Goal
What this topic is about and the current objective.

## Tasks
Current tasks ordered by priority. Include status if known.

## People
People working on this topic. Include role descriptions from "## Role Descriptions" if available.

## Constraints & Rules
Copy from "## Agent Constraints" section if present. If absent, omit this section entirely.

## Context
Parent project context relevant to this topic. Recent decisions, technical details, key files.

RULES:
- Stay focused on this topic. Skip unrelated parent project details.
- Output only information from the raw data.
- Language: match the raw data.

---

Raw data:
{raw_context}
