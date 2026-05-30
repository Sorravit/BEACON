---
name: web-research
description: Use when the user needs thorough, well-cited web research that synthesises multiple sources into a structured answer.
version: 1.0.0
author: BEACON
---

# Web Research Skill

A disciplined playbook for producing accurate, well-sourced research answers.

## When to use
- The user asks a question requiring current information, comparisons, or facts
  that should be verified across multiple independent sources.
- The user explicitly asks for "research", "find out", "compare", or "with sources".

## Procedure
1. **Clarify the goal.** Restate the research question in one sentence. Identify
   the key sub-questions that must be answered.
2. **Gather.** Use `web_search` for each sub-question. Prefer at least 3
   independent sources. For pages that need full content, open them with
   `browser_navigate` + `browser_get_text`.
3. **Cross-check.** Discard claims that appear in only one low-quality source.
   Flag any contradictions between sources explicitly.
4. **Synthesise.** Write a structured answer:
   - **Summary** — 2–4 sentence direct answer.
   - **Details** — organised by sub-question, with the reasoning.
   - **Sources** — bulleted list of the URLs actually used.
5. **State confidence.** End with a one-line confidence note (high/medium/low)
   and what would raise it.

## Quality bar
- Never invent URLs or citations. Only cite pages you actually retrieved.
- Distinguish facts from inference. Mark estimates clearly.
- If the question cannot be answered reliably, say so and explain why.

