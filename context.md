# Context

## Parent Context (bigboss)

# BigBoss Context Briefing

## Key People

**Max Nardit** — Owner. Timezone: Asia/Bangkok (UTC+7). Languages: Russian, English, German. Head of Data Analytics at Bobdo, freelance for Sekta. Building multi-agent system for agency automation. Family: wife Alexandra, daughters Ярослава (11) and Любава (8). Open source: agent-recall (PyPI).

**Ortwin Oberhauser** — CTO Bobdo, де-факто главный в агентстве. Location: Pattaya, Thailand (ICT, UTC+7). Telegram: @ortwino. Interested in OpenClaw, uses Gemini for strategy. Acknowledges he's not a PM, often changes direction. Son: Enzo. Daughter: Victoria. Wife: Fahmai.

**Matthias Koenig** — Co-CEO Bobdo. Location: Bregenz, Austria (CET). Video production, sales, client relations. Appointed PM for Josh Wise on Hotel Award (13.02.2026). Pragmatic style.

**Victoria Oberhauser** — Co-CEO Bobdo, Head of Social Media. Location: Bregenz (CET). Runs Meta/LinkedIn ads. Social media for Hotel Award. Finance LI videos to Enzo weekly.

**Enzo Oberhauser** — Performance Manager, Bobdo. Timezone: KGT (Kyrgyzstan, UTC+6). Telegram: @kilenzo. Google Ads specialist across multiple clients (budo7, etl-bodensee, luna, mawera, xiwine, zimm).

**Josh Wise** — Head of Development, Bobdo. Location: Bregenz (CET). Hotel Award scoring system developer. In Australia Feb 2026 (unavailable). Concerned about legal risks with Booking data.

**Serge (Sekta)** — Managing Director, Sekta. Telegram: @serjfedik. Decision-maker, direct communication style. Good relationship with Max.

**Markus Knestel** — CEO, Knestel. Strategic decisions on branding. Developing Knestron/KnestronX sub-brand for Leistungselektronik.

**Gunther (Zimm)** — ZIMM GmbH. Final say on 2026 budget. Friend of Ortwin. Quick decision-maker.

**Florian Wenger** — Zimm marketing/SEO contact. Works with Gaby on content and Wiki.

**Simona Rei** — Head of Marketing, Bendura Bank. Primary client contact.

## Agents & Projects

### Client Agents (Bobdo subclients)
| Agent | tmux | Status | Priority |
|-------|------|--------|----------|
| bobdo | bobdo | active | Agency operations, KI audit, Ortwin sim |
| gurgl | gurgl | active | New coordinators Julia + Lexi (Lisa on leave) |
| knestel | knestel | active | YouTube strategy, Marcel Schellhorn onboarded |
| hotel-award | hotelaward | active | Scoring system, Matthias as PM for Josh |
| wkv | wkv | active | Analytics & reports |
| budo7 | budo7 | active | Website, ads, Metin Kayar |
| tischbein | tischbein | active | WordPress, Atikon news import issues |
| zimm | zimm | active | SEO, Wiki, structured data, Geofencing |
| luna | luna | new | Google Ads, reporting |
| finance-li | financeli | new | LinkedIn, SEO, Google Ads |
| xiwine | xiwine | new | Google Ads, reporting |
| etl-bodensee | etlbodensee | new | Google Ads, reporting |
| bendura | bendura | new | Simona Rei contact, Josh involved |
| die-ofen-manufaktur | ofenmanufaktur | new | Bobdo subclient |
| mawera | mawera | new | Bobdo subclient |

### Client Agents (Independent)
| Agent | tmux | Status | Priority |
|-------|------|--------|----------|
| sekta | sekta | active | 1C server, VPN, site dev, multiple teams |

### Topics (Sub-sessions)
| Topic | Parent | Status | Focus |
|-------|--------|--------|-------|
| ki-audit | bobdo | open (ondemand) | AI strategy audit for Bobdo clients |
| ortwin-sim | bobdo | open | Ortwin simulation/communication |
| 1cvpn | sekta | open (ondemand) | VPN gateway for 1C server |
| camrelay | sekta | open (persistent) | RTSP→HLS camera streaming |
| claude-memory | bigboss | open | agent-recall open source package |
| spoony | alter-ego | open | Eco-activism: Spoon-billed Sandpiper |

### Personal Projects
| Agent | tmux | Status | Focus |
|-------|------|--------|-------|
| agency-dashboard | dashboard | active | Web dashboard (service) |
| telegram-sync | telegramsync | active | Telegram chat history sync |
| beetroot | beetroot | active | Landing & promotion |
| thailand-admin | thailandadmin | active | Visas, contracts, taxes |
| edu-tutor | edututor | dev | AI tutoring (family) |
| asset-inbox | assetinbox | active | File upload service |
| alter-ego | alterego | new | Personal agent |
| personal-site | personalsite | planning | max.nardit.com rebuild |
| gmail-sync | gmailsync | new | Gmail sync |
| google-ads-sync | adssync | new | Google Ads monitoring (MCC) |
| gsc-sync | gscsync | new | Search Console monitoring |
| ga4-sync | ga4sync | new | GA4 monitoring |

### System
| Agent | tmux | Status |
|-------|------|--------|
| bigboss | bigboss | this (orchestrator) |
| ticktick-agent | ticktickagent | active (service) |

## Active Priorities

### agent-recall Launch (High)
- v0.1.0 published on PyPI, v0.2.0 features done (strict_scopes, rename_scope, deep-chain context, per-agent overrides)
- **Next:** Write flagship article → Clean Twitter → Update LinkedIn → Launch day (article + threads) → HN Show + Reddit
- Repo: `~/projects/personal/claude-memory/` (PUBLIC). Rules: no internal names, end-of-session commits only

### Gurgl Transition
- Lisa on maternity leave since Feb 2026
- Replacements: Julia Dannenhauer (calls) + Lexi/Alexandra Thammer (Sölden/Ötztal)
- Ortwin positive about Julia (easy-going). Billing question open
- Vanessa Gstrein for urgent cases

### Knestel YouTube
- Marcel Schellhorn (videographer) onboarded 12.02.2026
- Judith to send YouTube strategy for feedback
- Markus Knestel away 16.02–04.03, Marcel coordinates with Judith

### Hotel Award
- Matthias Koenig appointed PM for Josh (13.02.2026)
- Josh in Australia Feb 2026. Legal concerns about Booking data for scoring
- Ting Yu working on hotel lists with Josh

### Sekta Infrastructure
- 1C server migration ongoing, VPN (1cvpn topic)
- Multiple specialists: Михаил Ищенко (1С), Иван St (networks), Александр Дидур + Андрей Вакарчук (Bitrix dev)
- Natalie Levchenko coordinating Yandex Metrika
- Airtable subscription cancelled (paid through 20.02.2026), CSV exported

### Tischbein
- Recurring news import problems (Atikon → WordPress). Last report: 05.02.2026

### Zimm
- Structured data implementation (Product, Author, BreadcrumbList, YouTube Video JSON-LD)
- Gaby tested Video schema with YouTube Data API v3 key (14.02)
- Geofencing campaigns active

## Context

- **System:** 30+ agents across tmux sessions. Hardware: AMD Ryzen 5 6600H, 32GB RAM, 468GB disk
- **Memory:** frames.db (SQLite, WAL) via MCP. AI briefings generated at 19:30 UTC via Haiku
- **Draft system:** All TickTick writes go through drafts → dashboard review. Security guard blocks direct API writes
- **Competitors:** Hubert Romberg — AI consulting agency in Vorarlberg, backed by Alpla, Blum, PTV Bank, Doppelmayr, Red Bull
- **Potential clients:** Fulterer (potential), Koller Metallbautechnik (KI audit case), Sebotics AG (KI audit case)
- **Thailand admin:** ATA Services handling visa/work permit. Kannika Sonsanga — main contact. Feb invoice: 226,235.45 THB due 25/02/2026
- **Spoony:** Family eco-activism project — Spoon-billed Sandpiper conservation at Pak Thale salt flats. Threat from Siam Gulf Petrochemical. Partners: BCST, Rainforest Trust, SBS Task Force

## Monitoring Points

### Services
| Service | Check |
|---------|-------|
| ticktick-bot.service | `systemctl status ticktick-bot` |
| asset-inbox.service | `systemctl status asset-inbox` |
| agency-dashboard.service | `systemctl status agency-dashboard` |
| caddy.service | `systemctl status caddy` |
| tmux-server.service | `systemctl --user status tmux-server` |

### Timers (UTC)
| Timer | Schedule | Purpose |
|-------|----------|---------|
| memory-review | 19:00 | Agents extract daily facts to frames.db |
| context-gen | 19:30 | AI briefing generation (Haiku) |
| nightly | 20:00 | Maintenance + sync + audit (Mon) + permissions (Fri) |
| kill-idle | 02:30 | Kill tmp-* tmux + idle Claude (>7d) |
| backup-srv | 03:00 | /srv/ → Google Drive |
| poll-tasks | hourly | TickTick Bigboss list check |

### Key Paths
- Memory DB: `~/.claude/memory/frames.db`
- Briefing cache: `~/.claude/memory/context_cache/`
- Obsidian vault: `/srv/shared/obsidian/`
- Secrets: `~/.secrets/`
- Logs: `/srv/log/` (activity, permissions, nightly, backup)
