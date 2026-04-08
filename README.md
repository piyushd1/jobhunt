# Job Hunt Agent

Multi-agent system that automates the tedious parts of job hunting. Discovers jobs across 7 portals, extracts JD details, scores them against your resume, finds relevant contacts at target companies, and drafts personalized outreach messages. Everything outputs to a Google Sheet for you to review and act on manually.

**The system never auto-applies, auto-sends, or clicks any Send/Apply/Submit/Connect button.**

## What It Does

```
Resume Profile ──> Source Jobs ──> Parse JDs ──> Match & Score ──> Find Contacts ──> Draft Messages ──> Google Sheet
     (once)        (7 portals)    (extract JD)  (hybrid AI)      (LinkedIn)        (referral ask)    (sorted output)
```

1. **Profiles your resume** once — extracts skills, experience, projects using LLM + a 91-skill PM taxonomy with embedding-based canonicalization
2. **Sources jobs** from LinkedIn, Naukri, Foundit, Indeed, Instahyre, Hirist, and Wellfound across your target locations
3. **Parses every JD** — visits each URL, extracts structured details, finds external apply links (Greenhouse, Lever, Workday, etc.)
4. **Scores and ranks** using a hybrid approach: deterministic formula (50% skills, 20% experience, 15% location, 15% domain fit) + LLM qualitative analysis with resume RAG
5. **Finds contacts** at shortlisted companies via LinkedIn (recruiters, hiring managers, HR) — verifies they currently work there
6. **Drafts referral-focused outreach** — personalized LinkedIn notes and emails using your actual project stories
7. **Writes to Google Sheets** — sorted by match score, with all links, scores, contacts, and drafts

## Quick Start

### Prerequisites

- Python 3.9+
- macOS (tested) or Linux
- API keys for at least one LLM provider (OpenRouter, Groq, OpenAI, or Anthropic)
- Google Cloud service account with Sheets API enabled

### 1. Clone and Install

```bash
git clone https://github.com/piyushd1/jobhunt.git
cd jobhunt
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your API keys:

```env
OPENROUTER_API_KEY=sk-or-...
GROQ_API_KEY=gsk_...
GOOGLE_CREDENTIALS_PATH=./credentials/your-service-account.json
GOOGLE_SHEET_ID=your-google-sheet-id
```

Place your Google service account JSON in `credentials/` and share the Google Sheet with the service account email (Editor access).

Edit `config.yaml` to set your search preferences (keywords, locations, experience range, etc.).

### 3. Set Up Browser Sessions

```bash
python setup_browser.py
```

This opens a Chromium browser with a dedicated profile. Log into each portal (LinkedIn, Naukri, etc.) when prompted. Sessions are saved and reused across runs.

### 4. Add Your Resume

```bash
cp /path/to/your/resume.pdf data/resume.pdf
```

Optionally, add a PM stories/projects file for richer matching:

```bash
cp /path/to/your/stories.md data/stories.md
```

### 5. Run

```bash
python -m src hunt          # Incremental run (keeps existing data)
python -m src hunt fresh    # Full fresh run (nukes DB, starts clean)
```

## CLI Commands

| Command | What it does |
|---|---|
| `python -m src hunt` | Run the full pipeline (incremental) |
| `python -m src hunt fresh` | Nuke DB + fresh run from scratch |
| `python -m src setup` | Re-open browser for portal logins |
| `python -m src status` | Show job counts, source breakdown |
| `python -m src models` | Show LLM model config per agent |
| `python -m src metrics` | Agent performance, costs, lead gen eval |
| `python -m src metrics all` | Full run history |
| `python -m src config` | Show search configuration |
| `python -m src config exp 7 13` | Set experience filter (7-13 years) |
| `python -m src config exp off` | Disable experience filter |
| `python -m src reset scores` | Reset match scores (re-score with current algo) |
| `python -m src reset all` | Full DB wipe for clean start |
| `python -m src blacklist show` | Show blocked companies/keywords |
| `python -m src blacklist add company "Acme"` | Block a company |
| `python -m src blacklist add keyword "intern"` | Block a title keyword |
| `python -m src blacklist remove <id>` | Remove a blacklist entry |

## Configuration

All settings are in `config.yaml`:

### Search Settings

```yaml
search:
  keywords:
    - "Product Manager"
    - "Senior Product Manager"
    - "Program Manager"
    # ... add your target roles
  locations:
    - "Bangalore"
    - "Hyderabad"
    - "Gurugram"
  experience_min: 7          # Target range
  experience_max: 13
  experience_buffer: 2       # Accepts 5-15 with ±2 buffer
  max_results_per_portal: 25

  role_priority:             # Affects scoring
    tier1: ["product manager", "senior product manager", "product owner"]
    tier2: ["program manager", "project manager"]
    tier3: ["growth manager", "business operations"]
```

### LLM Models (Per-Agent)

Each pipeline agent can use a different model, optimized for its task:

```yaml
llm:
  default_model: "openrouter/qwen/qwen3.6-plus:free"
  fallback_model: "openrouter/free"

  agents:
    resume_profiler:
      model: "openrouter/deepseek/deepseek-chat-v3-0324"   # Strong reasoning, runs once
      fallback: "openrouter/qwen/qwen3.6-plus:free"
    parsing:
      model: "openrouter/qwen/qwen3.6-plus:free"           # Fast extraction, 40+ calls
      fallback: "openrouter/free"
    matching:
      model: "openrouter/deepseek/deepseek-chat-v3-0324"   # Best reasoning for scoring
      fallback: "openrouter/qwen/qwen3.6-plus:free"
    messaging:
      model: "openrouter/qwen/qwen3.6-plus:free"           # Good writing quality
      fallback: "openrouter/free"
```

On rate limit errors: tries primary with exponential backoff (5s/15s/45s), then agent fallback, then global fallback.

### Matching Weights

```yaml
matching:
  weights:
    skills: 0.50
    experience: 0.20
    location: 0.15
    domain: 0.15            # Marketplace/B2C boost, B2B SaaS penalty
  shortlist_threshold: 70   # Lead gen only for scores >= this
```

### Domain Preferences

```yaml
matching:
  domain_preferences:
    strong_fit: ["marketplace", "consumer", "b2c", "adtech", "fintech", "ai"]
    moderate_fit: ["edtech", "logistics", "travel"]
    weak_fit: ["b2b saas", "enterprise software", "consulting"]
```

## How Scoring Works

```
Match% = 50% SkillOverlap + 20% ExperienceFit + 15% LocationFit + 15% DomainFit
         + Role tier boost (+10% for PM, -10% for stretch roles)
         + Mandatory skill cap (65% if >50% required skills missing)
```

- **Skill overlap**: Canonicalized against a 91-skill PM taxonomy with embedding-based fuzzy matching
- **Experience fit**: Graduated scoring (exact match = 100%, short by 1yr = 75%, short by 2yr = 45%)
- **Location fit**: Remote = 100%, city match = 100%, hybrid in target city = 70%
- **Domain fit**: Checks JD text for domain signals (marketplace/B2C = 100%, B2B SaaS = 20%)
- **LLM enhancement**: Retrieves relevant resume chunks via ChromaDB RAG, generates qualitative analysis

## Portals Supported

| Portal | Status | Notes |
|---|---|---|
| LinkedIn | Active | Jobs search + People search for contacts |
| Naukri | Active | Experience-filtered search |
| Foundit | Active | Multiple selector fallbacks |
| Indeed | Active | Indeed India (indeed.co.in) |
| Instahyre | Active | Tech-focused opportunities |
| Hirist | Active | Tech jobs portal |
| Wellfound | Active | Startup-focused jobs |
| WhatsApp | Planned | Group scanning for job links |

## Lead Generation

For jobs scoring above the threshold (default 70%), the system searches LinkedIn for contacts:

**3-strategy fallback:**
1. **Company Page → People tab** (most reliable — only shows current employees)
2. **LinkedIn People Search** with boolean operators (`Recruiter OR "Talent Acquisition"`)
3. **Text/link scanning** — regex extraction of profile URLs from page HTML

Contacts are verified as current employees by checking their headline for the company name. Former employees are skipped and tracked in evals.

**Rate limits:** 5-8 second random delay between searches, max 20 companies per day.

## Outreach Drafts

Messages are referral-focused:
- **LinkedIn note** (300 chars max): Warm, references one relevant project, clear ask for referral or chat
- **Email draft** (80-140 words): Why you're reaching out, relevant background, specific ask

Tone adapts to contact role (recruiter vs PM vs leadership). Uses ChromaDB RAG to reference your actual project stories.

## Observability

### Live Progress

During runs, a live terminal panel shows each stage with progress bars:

```
╭──── Job Hunt Pipeline run:a1b2c3d4 ────────────────────╮
│ Resume Profile   ✅ Done    1/1    44 skills extracted   │
│ Sourcing Jobs    ✅ Done    45     linkedin:22, naukri:18 │
│ Parsing JDs      ⏳ Running ████░░ 18/41  Sr PM @ Google │
│ Matching         ⏳                                      │
│ Lead Gen         ⏳                                      │
│ Drafting         ⏳                                      │
│ Google Sheets    ⏳                                      │
╰─────────────────────────────────────────────────────────╯
```

### Metrics Dashboard

```bash
python -m src metrics
```

Shows:
- **Run history**: Jobs found, parsed, errors, duration per run
- **Agent performance**: Success %, duration, items in/out per agent
- **Lead gen eval**: Coverage %, contacts per job, confidence breakdown, strategy stats
- **LLM costs**: Tokens and cost per agent per model

## Project Structure

```
jobhunt/
├── config.yaml                  # All runtime settings
├── .env                         # API keys (gitignored)
├── setup_browser.py             # One-time portal login helper
├── src/
│   ├── main.py                  # CLI entry point
│   ├── orchestrator.py          # Pipeline coordinator
│   ├── agents/
│   │   ├── base.py              # BaseAgent interface
│   │   ├── resume_profiler.py   # Resume → structured profile
│   │   ├── sourcing.py          # Find jobs across portals
│   │   ├── parsing.py           # Extract JD details
│   │   ├── matching.py          # Score and rank jobs
│   │   ├── leadgen.py           # Find LinkedIn contacts
│   │   └── messaging.py         # Draft outreach messages
│   ├── portals/
│   │   ├── base.py              # PortalAdapter interface
│   │   ├── linkedin.py
│   │   ├── naukri.py
│   │   ├── foundit.py
│   │   ├── indeed.py
│   │   ├── instahyre.py
│   │   ├── hirist.py
│   │   └── wellfound.py
│   ├── core/
│   │   ├── config.py            # Config loader
│   │   ├── browser.py           # Playwright wrapper
│   │   ├── db.py                # SQLite operations
│   │   ├── sheets.py            # Google Sheets writer
│   │   ├── llm.py               # LLM abstraction (litellm)
│   │   ├── embeddings.py        # Local embedding model
│   │   ├── skills.py            # PM skill taxonomy
│   │   └── vectorstore.py       # ChromaDB for resume RAG
│   └── eval/
│       ├── logger.py            # Structured JSON logging
│       ├── metrics.py           # Run metrics collector
│       └── progress.py          # Live terminal progress
├── data/
│   ├── resume.pdf               # Your resume (gitignored)
│   ├── stories.md               # PM stories for RAG (gitignored)
│   ├── browser_profile/         # Playwright sessions (gitignored)
│   └── job_hunt.db              # SQLite database (gitignored)
└── tests/
```

## Tech Stack

| Component | Library | Purpose |
|---|---|---|
| Browser automation | Playwright | Scraping portals with persistent sessions |
| LLM | litellm | Multi-provider abstraction (OpenRouter, Groq, OpenAI, Anthropic) |
| Database | SQLite | Source of truth for all job data |
| Resume RAG | ChromaDB + sentence-transformers | Semantic matching of resume to JDs |
| Embeddings | all-MiniLM-L6-v2 | Local, free, 384-dim vectors |
| Google Sheets | gspread + google-auth | Output projection layer |
| Skill taxonomy | Custom (91 PM skills) | Canonical skill matching with synonym groups |
| Config | PyYAML + python-dotenv | Settings + secrets |
| Logging | structlog | Structured JSON logs |
| Progress | rich | Live terminal progress display |

## Safety Guardrails

| Risk | Mitigation |
|---|---|
| Auto-send prevention | Hard block on Send/Apply/Submit/Connect buttons |
| CAPTCHA detection | Screenshot analysis, skips portal + alerts |
| LinkedIn rate limiting | 5-8s random delays, max 20 contact lookups/day |
| Account safety | Visible browser, random scroll patterns, dedicated profile |
| Former employee contacts | Current-company verification via headline check |
| Dedup | URL + fingerprint (company+title+location hash) across portals |
| Data privacy | All data local. Only LLM API calls go over network (text only) |

## License

Private project. Not for redistribution.
