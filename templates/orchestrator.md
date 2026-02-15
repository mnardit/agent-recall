You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — orchestrator/meta-agent managing all other agents.
This is the central coordinator of a multi-agent system.

Raw data from knowledge base:
{raw_context}

Create a high-level briefing for the orchestrator:

## Key People
PRIORITIZE by operational importance:
1. Owner — always first, with timezone and contacts
2. Key business contacts
3. Others — ONLY if they directly affect current active tasks
Do NOT list people with no operational role.

## Agents & Projects
Overview of active agents/projects grouped by category. Current status and priorities.

## Active Priorities
Cross-project priorities, blockers, deadlines. What needs attention NOW.
Group by urgency: critical > in progress > backlog.

## Context
Recent system-wide events, decisions, open questions.

## Monitoring Points
Key things to watch: services, timers, sessions, resources.

The orchestrator needs a BIRD'S EYE VIEW — don't go deep into any single project, focus on what matters across the whole system.
Language: match the raw data language.