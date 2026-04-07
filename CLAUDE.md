# Job Hunt Agent — Project Context

## What This Is

Multi-agent job hunting automation system. Discovers jobs across 8 portals + WhatsApp groups, extracts JD details, scores against resume using hybrid deterministic + LLM approach, finds relevant contacts, drafts personalized outreach. Outputs to Google Sheets. **Never auto-sends — user reviews and acts manually.**

## Target Roles

Product Manager, Senior PM, Group PM, Project Manager, Program Manager, Product Owner — all at tech companies.

## Architecture

**Phased MVP** — 3 phases, same clean codebase:

- **Phase 1** (current): Foundation + Sourcing + Parsing → Google Sheets with job listings from 3 portals (LinkedIn, Naukri, Foundit)
- **Phase 2**: Matching/ranking (hybrid formula + LLM + ChromaDB RAG) + remaining 5 portals (Indeed, Instahyre, Hirist, Wellfound, WhatsApp)
- **Phase 3**: Lead gen + messaging drafts + Telegram bot + full eval dashboard

**Pipeline:** Sequential async Python. Each agent is a module with `async run()` interface.

```
ResumeProfiler → SourcingAgent → ParsingAgent → MatchingAgent → LeadGenAgent → MessagingAgent → Google Sheets
```

## Tech Stack

| Component | Library |
|---|---|
| Browser | playwright (persistent profile in `data/browser_profile/`) |
| LLM | litellm (configurable: OpenAI, Anthropic, Google) |
| Database | sqlite3 (source of truth) |
| Resume RAG | chromadb (Phase 2) |
| Google Sheets | gspread + google-auth |
| Telegram | python-telegram-bot (Phase 3) |
| Config | pyyaml + python-dotenv |
| Logging | structlog (structured JSON) |

## Key Design Decisions

1. **SQLite is source of truth**, Google Sheets is output projection layer
2. **Hybrid matching**: deterministic formula (60% skills, 25% experience, 15% location) + LLM qualitative analysis
3. **Lead gen only for shortlisted jobs** (score >= 70%, max 20/day) to protect LinkedIn account
4. **Dedicated Playwright browser profile** — separate from daily browsing
5. **Multi-URL per job**: tracks all portal URLs + direct apply link (Greenhouse, Lever, etc.)
6. **Single-writer pattern**: only one component writes to Google Sheets, at end of pipeline
7. **Never auto-send**: blocked actions list prevents clicking Send/Apply/Submit/Connect

## Project Management

- **Linear** for issue tracking
- **GitHub** for code: https://github.com/piyushd1/jobhunt
- **Google Sheets** for output + feedback loop

## Current State

### Completed (Phase 1, Batch 1)
- [x] Step 1.1: Project scaffold (git, pyproject.toml, .gitignore, config.yaml, directories)
- [x] Step 1.2: Core infrastructure (config.py, browser.py, db.py, sheets.py, llm.py, logger.py, metrics.py)
- [x] Step 1.3: Browser setup helper (setup_browser.py) — user has logged into all portals

### Next Up (Phase 1, Batch 2)
- [ ] Step 1.4: Resume profiler agent
- [ ] Step 1.5: Portal adapter base + 3 adapters (LinkedIn, Naukri, Foundit)
- [ ] Step 1.6: Sourcing agent

### Remaining (Phase 1, Batch 3)
- [ ] Step 1.7: Parsing agent
- [ ] Step 1.8: Orchestrator + CLI
- [ ] Step 1.9: Google Sheets output

## File Structure

```
src/
├── agents/          # Pipeline agents (base, resume_profiler, sourcing, parsing, matching, leadgen, messaging)
├── portals/         # Portal adapters (base, linkedin, naukri, foundit, indeed, instahyre, hirist, wellfound, whatsapp)
├── core/            # Infrastructure (config, browser, db, sheets, llm)
└── eval/            # Observability (logger, metrics)
```

## How to Run

```bash
source .venv/bin/activate
python setup_browser.py          # One-time: log into portals
python -m src.main hunt          # Run pipeline (once orchestrator is built)
python -m src.main setup         # Re-run browser setup
```

## Configuration

- `config.yaml` — all runtime settings (portals, keywords, thresholds, weights)
- `.env` — secrets (API keys, Google credentials, Telegram token)
- Copy `.env.example` to `.env` and fill in values

## WhatsApp Groups (Phase 2)

Only these groups will be scanned: "PM Job's", "HireWire Job Alerts", "Protocol Jobs", "Daily PM Jobs"

## Plan File

Full implementation plan: `.claude/plans/optimized-questing-yao.md`
