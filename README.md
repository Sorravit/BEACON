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
playwrigh install chromium

# 2. Configure
cp config.example.env .env
# Edit .env with your API key and endpoint

# 3. Start Weaviate (vector memory)
docker compose up -d

# 4. Run
python main.py
```

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
| Notifications | `slack_notify` |
| Memory | `memory_list_facts`, `memory_add_fact`, `memory_delete_fact`, `memory_delete_research`, `memory_clear_research` |
| Background Tasks | `delegate_background_task`, `stop_background_task`, `background_task_status` |
| MCP (Playwright) | 21 additional browser automation tools |
| MCP (Atlassian) | Jira, Confluence, Compass integrations |

---

## ⚙️ Configuration

| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_KEY` | API key (OpenAI or IBM ICA) | required |
| `OPENAI_BASE_URL` | API endpoint | `https://api.openai.com/v1` |
| `AI_MODEL` | Model name | `gpt-3.5-turbo` |
| `AI_TEMPERATURE` | Response creativity (0-2) | `0.7` |
| `AI_MAX_TOKENS` | Max response tokens | `2000` |
| `SLACK_WEBHOOK_URL` | Slack webhook for notifications | optional |
| `TRANSFORMERS_OFFLINE` | Skip HuggingFace update checks | `1` (after first run) |

---

## 📁 Project Structure

```
main.py              — BEACON main entry point
agent_api.py         — Programmatic API wrapper
agent_executor.py    — Autonomous task execution engine
lib/
  vector_memory.py   — Weaviate-backed semantic memory
  mcp_client.py      — MCP protocol client
  agent_memory.py    — Simple key-value memory
docker-compose.yml   — Weaviate vector DB
examples/
  example_agent_usage.py  — Programmatic usage examples
scripts/             — Utility scripts
docs/                — Documentation
```

---

## 💬 Chat Commands

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
- [ ] Multi-agent orchestration
- [ ] Specialized sub-agents (researcher, coder, tester)
- [ ] Agent-to-agent communication
- [ ] Web dashboard for agent monitoring
- [ ] What ever Big wants to do

---

## 📚 Documentation

- [Getting Started](docs/GETTING_STARTED.md)
- [Configuration](docs/CONFIGURATION.md)
- [API Reference](docs/API_REFERENCE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

---

<p align="center">Built by Big · Powered by BEACON 🔦</p>
