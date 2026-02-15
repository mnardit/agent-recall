You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — focused topic/sub-session within a larger project.
Topics are temporary workstreams that need sharp focus.

Raw data:
{raw_context}

Create a focused briefing:

## Goal
What this topic is about and what needs to be done.

## Tasks
Current tasks, ordered by priority.

## People
Only people directly relevant to this topic.

## Context
Parent project context relevant to this topic.

Be very focused — topics are narrow. Skip anything not directly relevant.
Language: match the raw data language.