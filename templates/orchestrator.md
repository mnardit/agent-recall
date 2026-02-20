You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble, no changelogs, no "key updates" summaries. This is a fresh generation — do NOT reference previous versions. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — orchestrator/meta-agent managing all other agents.
Central coordinator of a multi-agent system.

INSTRUCTIONS (read before processing raw data):

Generate the COMPLETE briefing from scratch. This is a fresh generation, not an update. Do NOT reference any previous version or describe changes.

Focus on what matters across the whole system, not details of individual projects.

## Key People
Prioritize by operational importance:
1. Owner — first, with timezone and contacts
2. Key business contacts who affect multiple projects
3. Skip people with no current operational role.

## Agents & Projects
Overview grouped by category (clients, personal, system, topics). For each: current status and top priority.

## Active Priorities
Cross-project priorities, blockers, deadlines.
Group: urgent > in progress > backlog.

## Context
Recent system-wide events, decisions, open questions.

## Monitoring Points
Services, timers, sessions, resources to watch.

RULES:
- Generate the FULL briefing with all sections populated.
- Bird's eye view only. Do not go deep into any single project.
- Output only information from the raw data.
- Do NOT output changelogs, diffs, or summaries of what changed.
- Language: match the raw data.

---

Raw data from knowledge base:
{raw_context}
