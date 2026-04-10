# Job Hunt Agent — Project Context

> For another engineer or a new Claude session picking this up.

## What This Is

Multi-agent job hunting system for **Piyush Deveshwar** — a Group Product Manager with 12 years across marketplaces (Justdial, Urban Company, Gigstart). The system discovers PM jobs across 7 portals, scores them against his resume using hybrid AI matching, finds contacts at shortlisted companies, and drafts referral-focused outreach. Outputs to Google Sheets. **Never auto-sends or auto-applies.**

## Architecture

Sequential async Python pipeline. No framework (no LangGraph/CrewAI). Each agent is a module with `async run() → AgentResult`.

```
ResumeProfiler → SourcingAgent → ParsingAgent → MatchingAgent → LeadGenAgent → MessagingAgent → Google Sheets
```

## Current State (as of 2026-04-09)

**All 3 phases are implemented.** The system is functional end-to-end.

### What Works
- 7 portals active: LinkedIn, Naukri, Foundit, Indeed, Instahyre, Hirist, Wellfound
- 3-page pagination + 15-day date filter on all portals
- Resume profiling with 91-skill PM taxonomy + embedding canonicalization (all-MiniLM-L6-v2)
- Precision-first matching: required skills 25% + preferred skills 10% + experience 15% + location 10% + domain fit 25% + role fit 15%
- Domain scoring uses resume-signal matching plus disqualifier clusters, with score caps for severe domain mismatch or missing required skills
- ChromaDB RAG with resume chunks + PM stories for contextual matching
- Lead gen via LinkedIn: Company Page → People tab (current employees only) + People Search fallback
- Referral-focused message drafting (LinkedIn notes + emails)
- Per-agent LLM model selection with fallback chains (OpenRouter primary)
- Live terminal progress display (rich)
- CLI: hunt, hunt fresh, reset, config, status, models, metrics, blacklist

### Known Issues / Next Steps
- Foundit and some newer portals return 0 results (selectors may need updating against live DOM)
- Lead gen success rate varies — LinkedIn rate-limits People search aggressively
- WhatsApp adapter not built yet (planned for group link scanning)
- Experience standardization in the sheet could be tighter
- Telegram bot not built yet

### Key Design Decisions
1. **SQLite is source of truth**, Google Sheets is a filtered projection (only jobs ≥ sheet_min_score)
2. **Precision-first ranking favors real fit over broad recall** — sourcing only keeps PM core + TPM/PgM families by default, and matching separates required vs preferred skills
3. **Domain fit is capped, not just weighted** — obvious domain mismatches stay visible but get sharply reduced totals via domain-risk caps
4. **B2B marketplace is fine** (still marketplace dynamics), pure B2B SaaS is not
5. **Consumer fintech is fine** (PhonePe, CRED), core banking/insurance is not
6. **Lead gen only for ≥70% match**, max 20 companies/day, 5-8s delays
7. **Never auto-send**: blocked actions list in browser.py prevents clicking Send/Apply/Submit/Connect
8. **Per-agent models**: parsing uses fast/cheap model, matching uses strong reasoning model

## Config Quick Reference

```yaml
# Key settings in config.yaml:
search.pages_per_search: 3          # Pages per keyword+location combo
search.max_age_days: 15             # Only jobs from last 15 days
search.experience_min/max: 7/13     # With ±2 buffer = accepts 5-15 years
search.allowed_role_families: [pm_core, tpm_pgm]
matching.sheet_min_score: 60        # Only jobs ≥60% in Google Sheet
matching.weights: {required_skills: 0.25, preferred_skills: 0.10, experience: 0.15, location: 0.10, domain: 0.25, role_fit: 0.15}
matching.shortlist_threshold: 70    # Lead gen only for ≥70%
```

## File Structure

```
src/
├── main.py              # CLI entry (hunt, reset, config, status, models, metrics, blacklist)
├── orchestrator.py      # Pipeline coordinator with live progress
├── agents/
│   ├── base.py          # BaseAgent + AgentResult
│   ├── resume_profiler.py  # PDF → structured JSON with skill canonicalization
│   ├── sourcing.py      # Run portal adapters, dedup, experience filter, blacklist
│   ├── parsing.py       # Visit URLs, extract JD, find apply links
│   ├── matching.py      # Hybrid scoring (formula + LLM + RAG)
│   ├── leadgen.py       # LinkedIn People search (3 strategies)
│   └── messaging.py     # Draft referral-focused outreach
├── portals/
│   ├── base.py          # PortalAdapter interface, health checks
│   ├── linkedin.py, naukri.py, foundit.py, indeed.py, instahyre.py, hirist.py, wellfound.py
│   └── __init__.py      # Registry mapping names → classes
├── core/
│   ├── config.py        # YAML + .env loader
│   ├── browser.py       # Playwright persistent context, anti-detection, lock cleanup
│   ├── db.py            # SQLite schema + CRUD
│   ├── sheets.py        # Google Sheets writer (filtered by sheet_min_score)
│   ├── llm.py           # litellm wrapper with per-agent models + backoff + fallback chains
│   ├── embeddings.py    # Local sentence-transformers (all-MiniLM-L6-v2)
│   ├── skills.py        # 91-skill PM taxonomy + SkillCanonicalizer
│   └── vectorstore.py   # ChromaDB for resume + stories RAG
└── eval/
    ├── logger.py        # structlog JSON logging
    ├── metrics.py       # Per-agent timing/success tracking
    └── progress.py      # Rich live progress panel

data/ (gitignored)
├── resume.pdf           # User's resume
├── stories.md           # PM stories for RAG (Amazon LP cheatsheet)
├── candidate_profile.json  # Cached parsed resume
├── job_hunt.db          # SQLite database
├── browser_profile/     # Playwright sessions
└── chroma/              # ChromaDB embeddings
```

## How to Run

```bash
source .venv/bin/activate
python -m src hunt              # Incremental run
python -m src hunt fresh        # Full reset + fresh run
python -m src status            # Job counts
python -m src metrics           # Agent performance + costs
python -m src config            # Search settings
python -m src config exp 7 13   # Set experience filter
python -m src models            # LLM config per agent
python -m src blacklist show    # Blocked companies/keywords
```

## Candidate Profile Summary

Piyush Deveshwar — 12 years, Group Product Manager at Justdial.
- **Core domains**: Marketplace, B2C, Consumer Platforms, Local/Home Services, AI/ML
- **Strengths**: 0-to-1 building, P&L ownership, marketplace dynamics, LLM product development, growth
- **Companies**: Justdial (marketplace), Urban Company (home services), Gigstart (artist marketplace)
- **NOT his domain**: B2B SaaS, enterprise software, core banking, insurance, hardware, telecom
- **Target roles**: PM, Senior PM, GPM, Product Owner, Program Manager, TPM
- **Locations**: Bangalore (primary), Hyderabad, Gurugram
- **Open to**: Consumer fintech, B2B marketplaces, AI product roles

## GitHub

https://github.com/piyushd1/jobhunt
