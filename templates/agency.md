You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — manages an agency/organization.
This agent oversees multiple sub-clients and coordinates team work.

Raw data:
{raw_context}

Create a structured briefing:

## Team
Team members: roles, responsibilities, how to reach them.

## Clients & Projects
Active clients with current status and priorities.

## Current Tasks
Cross-client tasks and agency-level priorities.

## Context
Recent events, decisions, open questions.

Language: match the raw data language.