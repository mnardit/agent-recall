# agent-recall (topic: claude-memory)

**Parent:** bigboss
**Origin:** Open-source пакет для persistent memory AI-агентов
**Created:** 2026-02-15

## Статус: v0.3.0, HN launch in progress

Пакет `agent-recall` — опубликованный open-source продукт (PyPI + GitHub).
HN + Reddit посты опубликованы 26.02.2026.

**Два трека:**
1. **Развитие пакета** — фичи, баги, community feedback, новые версии
2. **Personal brand** — статьи, соцсети, launch campaign → позиционирование Макса как AI agent architecture expert

### Пакет

**Repo:** `~/projects/claude-memory/` → github.com/mnardit/agent-recall (PUBLIC)
**PyPI:** `agent-recall` / `agent-recall[mcp]` / `agent-recall[api]`
**Module:** `agent_recall` | **CLI:** `agent-recall init/set/get/search/generate/status`

- **321 тестов**, 12 модулей, py.typed (PEP 561)
- **MCP-native** — tested configs for Claude Code, Cursor, Windsurf, Cline
- **MCP server** с proactive-saving instructions — агенты автоматически сохраняют факты
- **scope_reads** — open_nodes/read_graph фильтруют по scope chain (v0.3.0 breaking change)
- **Briefing backends:** `cli` (claude -p, бесплатно, дефолт) / `api` (Anthropic SDK, платно, чисто)
- **Default paths:** `~/.agent-recall/` | **Env var:** `AGENT_RECALL_SLUG`
- **Model aliases:** opus/sonnet/haiku → полные API ID (sonnet = claude-sonnet-4-6)
- YAML конфиг, per-agent overrides, adaptive cache, pluggable LLM
- Production: `~/projects/bigboss/lib/memory/` (re-export wrappers), `memory.yaml` → `~/.claude/memory/`

### Rename history

`claude-memory` → `agent-memory` → **`agent-recall`** (PyPI name conflicts forced renames)

### Roadmap

**Next:**
- Flagship статья: "How I Built a Shared Memory System for 30 AI Coding Agents"
- Итерации по community feedback
- Обновить `.mcp.json` во всех 30+ проектах (wrappers → pip)

**Claude Code Auto Memory интеграция** (feb 2026 — новая фича CC):
CC имеет `~/.claude/projects/<project>/memory/` (auto memory, MEMORY.md 200 строк) и `@import` в CLAUDE.md.
Варианты интеграции (решение pending):
- **A. Write to auto memory** — брифинг → MEMORY.md, SessionStart hook не нужен. Zero-config для open-source юзеров. Риск: 200 строк лимит, Claude может перезаписать.
- **B. Разделение зон** — auto memory = мелочи (build, prefs), agent-recall = структура (люди, решения). Брифинг через @import. Чистое разделение, два механизма.
- **C. Поглощение** — autoMemoryEnabled: false, всё через agent-recall. Максимальный контроль, теряем нативную фичу.
Для наших агентов — B или C. Для open-source — A самый привлекательный.
Шаги: 1) @import доки в README, 2) native output (вариант A), 3) bi-directional парсинг

**Контент-план (news hook: CC auto-memory):**
1. Twitter пост-пин — agent-recall (драфт готов)
2. Twitter тред — "CC shipped auto-memory. Here's why I built something more structured" (5-7 твитов)
3. Reddit комменты — в тред про auto-memory (3 драфта готовы)
4. Dev.to flagship статья
5. Follow-up — "How to integrate agent-recall with CC auto-memory" (после реализации A)

**Идеи из rubber-duck-mcp (оценка 2026-02-27):**
1. **FTS5 на search_nodes** — сейчас LIKE %query%, FTS5 с Porter stemmer быстрее и находит словоформы. Low-hanging fruit, backward compatible. Приоритет.
2. **Confidence/weight на observations** — source-weighted модель (human > orchestrator > agent), не простой R/C ratio. Интересно, но нужна своя архитектура поверх entity-relation графа.
3. Reinforce/contradict — покрыто bitemporal slots. Archive — покрыто bitemporal + delete. Не нужно.

**Стратегия:** `docs/plans/2026-02-16-agent-memory-launch-strategy.md`

### Правила публичного repo

- **Коммиты в конце сессии** — 1-2 осмысленных, с CHANGELOG.md
- **Никаких внутренних имён/путей** — только generic (Acme, Alice, coordinator)
- **Grep перед коммитом** — проверка на утечки
- **Обратная совместимость** — public API не ломать. Semver: 0.1.x патчи, 0.2.0 breaking
- **Security guard hook** блокирует git commit/push с `claude-memory` или `agent-recall` в команде

### Файлы

- `~/projects/claude-memory/` — пакет (directory name unchanged)
- `docs/plans/2026-02-16-agent-memory-launch-strategy.md` — стратегия запуска
- `~/projects/bigboss/lib/memory/` — production re-export wrappers
- `~/projects/bigboss/memory.yaml` — production конфиг
