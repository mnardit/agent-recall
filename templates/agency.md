You are generating a context briefing for an AI agent. The briefing will be injected into the agent's system prompt at startup.

IMPORTANT: Output ONLY the briefing content. No meta-commentary, no preamble, no changelogs, no "key updates" summaries. This is a fresh generation — do NOT reference previous versions. Use markdown formatting. Maximum {budget} characters.

Agent: "{slug}" — manages an agency/organization with multiple sub-clients.

INSTRUCTIONS (read before processing raw data):

Generate a briefing with these sections:

## Team
All team members with: name, role, contact method, timezone, key traits.
Use role descriptions from "## Role Descriptions" if available.
Format as a table if 5+ people.

## Clients & Projects
Active clients grouped by priority. For each: status, current focus, key contact.
Prioritize by urgency and business importance.

## Current Tasks
Cross-client and agency-level tasks. Group by area.

## Constraints & Rules
Copy from "## Agent Constraints" section if present. If absent, omit this section entirely.

## Context
Recent events, decisions, open questions that affect the agency.

RULES:
- Output only information from the raw data. Do not add external knowledge.
- Language: match the raw data.

---

Raw data:
{raw_context}
