
# 🔦 BEACON
### Big's Executing Agent for Control, Automation & Orchestration Network

**Version:** 4.2.0 | **Status:** Production Ready | **Author:** Big (Sorravit)

> BEACON is a personal autonomous AI agent that executes tasks, automates workflows, browses the web, manages files, and remembers everything — designed to evolve into a full multi-agent orchestration system.

---

## ✨ What is BEACON?

BEACON started as a single intelligent agent and is built to grow. Whether running solo or orchestrating a network of specialized agents, BEACON remains your central command — the lighthouse that guides every task to completion.

| Today | Tomorrow |
|-------|----------|
| Single autonomous agent | Multi-agent orchestration network |
| Personal task automation | Distributed intelligent services |
| Tool execution & memory | Agents with specialized roles |
| One BEACON | Many agents, one BEACON at the center |

---

## 🚀 Quick Start

```bash
# 1. Setup
git clone <repository>
cd beacon
python3 -m venv venv
source venv/bin/activate.fish   # fish shell

pip install -r requirements.txt
playwright install chromium

# 2. Configure
cp config.example.env .env
# Edit .env with your API key and endpoint

# 3. Start Weaviate (vector memory)
docker compose up -d

# 4. Run (CLI mode)
python main.py

# 4b. Run (Web UI mode)
python web_app.py
# Then open http://localhost:8000

# 5. Run tests
python -m pytest -q
```

---

## 🧠 Model Selection

BEACON can use any of the models declared in **`models.yaml`** — a single,
curated, offline source of truth (no network discovery). Each model has an id,
label and optional description/tags, and every agent *role* has a default model.

- **Per-chat model** — pick a model from the header dropdown in the web UI. The
  choice is remembered per browser.
- **Per-agent models** — in Orchestrate mode each spawned agent (researcher,
  planner, engineer, devops, verifier) uses its role default automatically, and
  you can override any of them manually (right-click the 🧠 toggle → *Agent
  Models*).
- **Endpoints** — `GET /models` returns the catalog + role defaults;
  `POST /models/reload` hot-reloads `models.yaml`.

```yaml
# models.yaml (excerpt)
default: global/anthropic.claude-sonnet-4-6
roles:
  researcher: global/gpt-5.1-chat
  verifier:   global/anthropic.claude-sonnet-4-5-20250929-v1:0
models:
  - id: global/anthropic.claude-sonnet-4-6
    label: Claude Sonnet 4.6
    tags: [default, reasoning, coding]
```

---

## 🎓 Agent Skills (`SKILL.md`)

BEACON supports **Anthropic-style Agent Skills**: drop a folder under `skills/`
with a `SKILL.md` (YAML frontmatter + instructions) and BEACON loads it with
**progressive disclosure** — only the name + description sit in the system
prompt; the full playbook is pulled in on demand via the `load_skill` tool.

```
skills/
  web-research/
    SKILL.md          # ---\nname/description/version\n---\n# instructions
    scripts/...       # optional bundled resources
```

See [`skills/README.md`](skills/README.md) for the full format.

---

## 🤖 Agent Mode — Multi-Agent Orchestration

Enable the 🧠 **Agent Mode** toggle to run a goal through a coordinated team of
specialist sub-agents, looping until the work passes verification:

```
RESEARCH → PLAN → ACT (engineer / devops / k8s / …) → VERIFY
   ▲                                                     │
   └──────────────── feedback on failure ────────────────┘
```

- **Fixed core roles + dynamic spawning** — researcher, planner and verifier are
  always present; the planner picks the right ACT specialist (lead software
  engineer, devops, kubernetes, data engineer, …) for each task.
- **Spec-aware verification** — when the goal references a Jira issue, research
  and verification fetch the issue, its acceptance criteria, linked tickets and
  related Confluence pages via MCP and validate against them.
- **Per-agent models** — each role resolves its model from `models.yaml`, with
  optional manual overrides (right-click the 🧠 toggle → *Agent Models*).
- **Endpoint** — `POST /agent/orchestrate` with `{description, session_id,
  max_rounds, model_overrides}`; progress streams via `/agent/task/{id}/stream`.

### What each agent passes to the next (context hand-off)

The data flow between agents is explicit and visible (logged as `HANDOFF` lines
and shown in the progress card as 🔄 entries):

| From → To | What is passed |
|---|---|
| Research → Planner | Research findings (facts, constraints, spec) + prior chat context |
| Planner → ACT specialist | Chosen specialist, acceptance criteria, ordered steps |
| ACT → Verifier | The work product + acceptance criteria (and the spec/Jira refs) |
| Verifier → Research (next round) | Failure feedback, so the team re-approaches with the reason it failed |

Within ACT, each step also receives the **running context** (goal + research +
all prior step results) so the specialist stays coherent across steps.

Watch the live log to see who is doing what:

```
[orch_…] ROUND 1/2
[orch_…] SPAWN    Research Agent          model=global/gpt-5.1-chat tools=True
[orch_…] RESEARCH done | 1820 chars | The repo uses FastAPI…
[orch_…] HANDOFF  Research Agent → Project Planner | research findings (1820 chars)
[orch_…] SPAWN    Project Planner         model=global/anthropic.claude-sonnet-4-6 tools=False
[orch_…] PLAN done | specialist=devops | 4 step(s) | 3 criteria
[orch_…] HANDOFF  Project Planner → devops (ACT) | execution plan (…)
[orch_…] ACT step 1/4 | Devops | Create the Dockerfile…
…
[orch_…] VERIFY done | passed=true | All criteria met…
```

> **Agent Mode vs the old Task Mode.** Agent Mode replaces the previous
> single-agent Task Mode. Task Mode ran the *same* research→plan→act→verify loop
> but as **one** agent using **one** model. Agent Mode runs it as a **team** —
> each phase is a separate role-scoped agent with its **own selectable model**,
> the planner spawns the right specialist dynamically, and verification can
> validate against Jira/Confluence. The legacy `POST /agent/task` endpoint
> remains for backward compatibility but is no longer used by the UI.


---

## 🌐 Web Interface


BEACON includes a full web UI served by FastAPI on port 8000.

### Features

- **Multi-session chat** — each conversation is a separate session with its own history, persisted to `sessions/` and survives server restarts
- **Pinned chats** — pin important sessions to the top of the sidebar
- **Drag-to-reorder pins** — reorder pinned sessions by dragging in the sidebar
- **File drag-and-drop** — drag files directly into the chat window to attach them (uploaded to `temp/`)
- **Message timestamps** — each message shows when it was sent
- **Copy code button** — code blocks have a one-click copy button
- **Background task panel** — view, monitor, and stop all running background tasks from the ⚙ Tasks button
- **Background task notifications** — messages from background scripts are injected live into chat using `NOTIFY:`, `ALERT:`, `SUCCESS:`, or `WARNING:` prefixes
- **SSE for task updates** — real-time background task status and notifications via Server-Sent Events (`GET /events`)
- **Agent activity indicator** — shows what tool the agent is currently executing
- **Stop button** — cancel a running AI response mid-execution
- **Smart session titles** — first message auto-generates a concise title via the AI model
- **Rename / delete sessions** — rename any chat from the sidebar or delete it
- **Optional auth** — set `AUTH_TOKEN` in `.env` to protect the UI with a login page

### Web Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Web chat UI |
| `POST /chat/stream` | Send a message (SSE streaming response) |
| `GET /chat/reconnect/{id}` | Reconnect to a running agent task |
| `POST /chat/stop/{id}` | Cancel a running agent task |
| `GET /chat/status/{id}` | Check if agent is running for a session |
| `POST /chat/clear` | Clear conversation history for a session |
| `GET /sessions` | List all sessions (pinned first, then by date) |
| `POST /sessions` | Create a new session |
| `GET /sessions/{id}` | Get session history |
| `PATCH /sessions/{id}/rename` | Rename a session |
| `PATCH /sessions/{id}/pin` | Toggle pin on a session |
| `PATCH /sessions/reorder-pins` | Reorder pinned sessions |
| `DELETE /sessions/{id}` | Delete a session |
| `POST /upload` | Upload a file (saved to `temp/`) |
| `GET /events` | SSE stream: task status + notifications + agent activity |
| `GET /tasks` | List background CLI tasks |
| `POST /tasks/{name}/stop` | Stop a background task |
| `POST /tasks/stop-all` | Stop and clear all background tasks |
| `DELETE /tasks/{name}/log` | Clear a task's log file |
| `GET /tasks/{name}/logs` | SSE stream of a task's log output |
| `GET /health` | Health check |

---

## 🧠 Vector Memory (Weaviate)

BEACON has persistent semantic memory backed by Weaviate. It remembers across sessions — you never have to repeat yourself.

- **Personal facts** — tell BEACON things about you once, it remembers permanently
- **Research memory** — every tool result is stored and retrieved semantically, so BEACON never re-fetches what it already knows
- **Memory tools** — `memory_list_facts`, `memory_add_fact`, `memory_delete_fact`, `memory_delete_research`, `memory_clear_research`

Weaviate runs in Docker. Start it with `docker compose up -d`.

---

## 🔧 Available Tools

| Category | Tools |
|---|---|
| System | `get_current_time`, `execute_command` |
| Files | `read_file`, `write_file`, `list_files` |
| Web | `web_search`, `browser_navigate`, `browser_click`, `browser_type`, `browser_screenshot`, `browser_get_text`, `browser_close` |
| HTTP | `http_get`, `http_post` |
| Background Tasks | `delegate_background_task`, `stop_background_task`, `background_task_status` |
| MCP Management | `mcp_list_servers`, `mcp_restart_server`, `mcp_restart_all` |
| Memory | `memory_list_facts`, `memory_add_fact`, `memory_delete_fact`, `memory_delete_research`, `memory_clear_research` |
| MCP (Playwright) | 21 additional browser automation tools |
| MCP (Atlassian) | Jira, Confluence, Compass integrations |

### Search

`web_search` uses the **DuckDuckGo Instant API with Google browser fallback**:

```
web_search(query)
    |
    +-- DuckDuckGo Instant Answer API (no API key, no rate limits)
            |
            +-- Results found  → return DuckDuckGo results
            +-- No results     → fall through
                    |
                    +-- Open Google in browser, return page text
```

---

## 🔄 Background Tasks

BEACON can delegate long-running or infinite-loop tasks to background processes via `delegate_background_task`. Background scripts communicate back to the chat window using special print prefixes:

| Prefix | Chat appearance |
|---|---|
| `print('NOTIFY: <text>')` | Info message |
| `print('SUCCESS: <text>')` | Green success message |
| `print('WARNING: <text>')` | Yellow warning |
| `print('ALERT: <text>')` | Red urgent alert |

The web server polls `logs/notify_*.json` every 3 seconds via the SSE `/events` stream and injects matching messages into the correct chat session. Plain `print()` without a prefix goes only to the log file.

**CLI usage:**
```bash
python scripts/background_task.py --name "my_task" --command "python my_script.py" --interval 60
python scripts/background_task.py --stop --name "my_task"
python scripts/background_task.py --status
```

---

## ⚙️ Configuration

| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API key | required |
| `OPENAI_BASE_URL` | API endpoint | `https://api.openai.com/v1` |
| `AI_MODEL` | Model name | `gpt-3.5-turbo` |
| `AI_TEMPERATURE` | Response creativity (0-2) | `0.7` |
| `AI_MAX_TOKENS` | Max response tokens | `2000` |
| `TRANSFORMERS_OFFLINE` | Skip HuggingFace update checks | `1` (after first run) |
| `AUTH_TOKEN` | Protect web UI with token-based login | empty (auth disabled) |
| `WEAVIATE_PORT` | Weaviate vector DB port | `8090` |

---

## 📁 Project Structure

```
main.py              — BEACON main entry point (CLI + AIAgent + Config + model registry)
web_app.py           — FastAPI web server + SSE streaming + session management
models.yaml          — Curated model registry (selectable models + role defaults)
api/
  agent_api.py       — Programmatic API wrapper
  agent_executor.py  — Single-agent Research→Plan→Act→Verify engine
core/
  models.py          — ModelRegistry (loads models.yaml)
  skills.py          — SkillManager (Agent Skills / SKILL.md, progressive disclosure)
  orchestration/     — Multi-agent orchestration
    roles.py         — Role definitions + dynamic specialist builder
    sub_agent.py     — Role-scoped sub-agent (per-role model)
    orchestrator.py  — Research→Plan→Act→Verify team loop
  vector_memory.py   — Weaviate-backed semantic memory
  mcp_client.py      — MCP protocol client
  agent_memory.py    — Simple key-value memory
skills/              — Agent Skills (each subfolder has a SKILL.md)
utils/
  encoding.py        — Shared UTF-8 safe encoding helper
docker-compose.yml   — Weaviate vector DB
scripts/
  background_task.py — Background task runner (delegate_background_task backend)
  run_agent.py       — Standalone agent runner
  run_interactive_task.py — Interactive task runner
tools/
  manager.py         — Tool registry/composition entrypoint
  base/              — Time/search/command/background task tools
  browser/           — Browser automation tools
  file/              — File read/write/list tools
  web/               — HTTP tools
  mcp/               — MCP server management tools
  memory/            — Vector-memory management tools
  skill/             — Agent Skills tools (list_skills, load_skill)
static/
  index.html         — Web UI shell
  app.js             — Frontend logic
  style.css          — Styles
sessions/            — Persisted chat session JSON files
logs/                — Agent + background task logs
examples/
  example_agent_usage.py  — Programmatic usage examples
docs/                — Documentation
tests/               — Regression tests for core utilities
```

---

## 💬 Chat Commands (CLI mode)

| Command | Action |
|---|---|
| `clear` | Clear conversation history |
| `quit` / `exit` | Exit BEACON |
| `help` | Show usage examples |

---

## 🗺️ Roadmap

- [x] Single autonomous agent
- [x] Persistent vector memory (Weaviate)
- [x] Browser automation (Playwright)
- [x] MCP integrations (Jira, Confluence)
- [x] Background task delegation
- [x] Web UI with multi-session chat
- [x] Pinned chats & drag-to-reorder
- [x] File drag-and-drop upload
- [x] SSE-based real-time task notifications
- [x] Multi-model selection (per-chat + per-agent)
- [x] Agent Skills (`SKILL.md`, progressive disclosure)
- [x] Multi-agent orchestration (research → plan → act → verify loop)
- [x] Specialized sub-agents (researcher, planner, engineer, devops, verifier)
- [x] Spec-aware verification (Jira / Confluence)
- [ ] Agent-to-agent communication
- [ ] Whatever Big wants to do

---

## 📚 Documentation

- [Getting Started](docs/GETTING_STARTED.md)
- [Configuration](docs/CONFIGURATION.md)
- [API Reference](docs/API_REFERENCE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

---


---

## Telemetry

BEACON ships built-in OpenTelemetry tracing. Every tool call is auto-wrapped in an OTel span with tool.duration_ms.

**Quick start:**

    docker compose -f docker-compose.yml -f docker-compose.telemetry.yml up -d
    echo OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 >> .env
    python web_app.py
    open http://localhost:16686

**Key env vars:** OTEL_EXPORTER_OTLP_ENDPOINT (default: http://localhost:4317), BEACON_OTEL_CONSOLE (default: false), SESSION_REPORT_DIR (default: /tmp/beacon_sessions).

**Disable:** leave OTEL_EXPORTER_OTLP_ENDPOINT unset and BEACON_OTEL_CONSOLE=false. Zero overhead.

Per-session JSON reports: output/sample_trace.json (reference). Full guide: output/telemetry_additions.md

<p align="center">Built by Big · Powered by BEACON 🔦</p>
