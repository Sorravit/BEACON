# BEACON Agent Skills

A **skill** is a folder containing a `SKILL.md` file that packages domain
expertise the agent can load on demand. Skills use **progressive disclosure**:
only each skill's `name` and `description` are kept in the system prompt; the
full instruction body is loaded by the model via the `load_skill` tool when a
task matches the skill.

## Layout

```
skills/
  my-skill/
    SKILL.md          # required — frontmatter + instructions
    scripts/helper.py # optional — bundled resources
    reference.md      # optional — bundled resources
```

## SKILL.md format

```markdown
---
name: my-skill
description: One sentence describing exactly when to use this skill.
version: 1.0.0
author: you
---

# My Skill

Detailed, step-by-step instructions the agent should follow when this skill
is loaded. Reference bundled files by relative path (e.g. `scripts/helper.py`).
```

### Frontmatter fields

| Field         | Required | Notes                                            |
|---------------|----------|--------------------------------------------------|
| `name`        | no\*     | Defaults to the folder name if omitted.          |
| `description` | yes      | Shown in the catalog; make it a clear trigger.   |
| `version`     | no       | Informational.                                   |
| (others)      | no       | Any extra keys are kept in the skill's metadata. |

\* If `name` is omitted the folder name is used.

## How the agent uses skills

1. At startup BEACON scans `skills/*/SKILL.md` and injects the catalog
   (names + descriptions) into the system prompt.
2. When a request matches a skill, the model calls
   `load_skill(name="my-skill")` to retrieve the full body.
3. The model follows the returned instructions, optionally using bundled
   resources listed at the end of the loaded text.

Reload skills without restarting by restarting the agent process (skills are
discovered during `AIAgent.initialize()`).

