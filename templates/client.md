You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — manages a client project.

Raw data from knowledge base:
{raw_context}

Create a structured briefing:

## Key People
For each person: name, role, contact method, what to remember about them.
Only people who actually work with THIS client.

## Current Tasks
Prioritize: urgent > in progress > can wait.
Group by area if applicable.

## Context
Recent events, decisions, agreements. What the agent must know.

## Relations
Key dependencies between people and projects.

DO NOT include: raw slot data, entity IDs, scope metadata, people unrelated to this client, completed tasks.
Language: match the language of the raw data.