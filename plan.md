# Multi-Agent Job Hunt Automation System: Complete Design Blueprint

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        TELEGRAM CONTROL PLANE                       │
│   /hunt → /status → /set_keywords → /config_show                   │
│   Progress callbacks + Excel file delivery                          │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      LOCAL ORCHESTRATOR                             │
│   Pipeline supervisor | State manager | Error handler               │
└───┬──────────┬──────────┬──────────┬──────────┬────────────────────┘
    │          │          │          │          │
    ▼          ▼          ▼          ▼          ▼
┌────────┐ ┌────────┐ ┌──────────┐ ┌─────────┐ ┌──────────┐
│Sourcing│ │Parsing │ │Matching &│ │Lead Gen │ │Messaging │
│ Agent  │ │ Agent  │ │ Ranking  │ │ Agent   │ │  Agent   │
│(Browser)│ │(Browser│ │(LLM)     │ │(Browser)│ │  (LLM)   │
│        │ │+LLM)   │ │          │ │         │ │          │
└────────┘ └────────┘ └──────────┘ └─────────┘ └────┬─────┘
                                                       │
                                                       ▼
                                              ┌────────────────┐
                                              │  EXCEL OUTPUT  │
                                              │ job_hunt_      │
                                              │ YYYY-MM-DD.xlsx│
                                              └────────────────┘
```

---

## Core Design Principles

1. **Deterministic automation for navigation, LLMs for intelligence** — Browsers handle clicking, scrolling, and extraction; AI handles parsing, matching, reasoning, and drafting.
2. **Resume parsed once, cached indefinitely** — A structured candidate profile is built once from your PDF and reused across every run.
3. **SQLite as source of truth, Excel as review layer** — Prevents workbook corruption, enables retries, and keeps Excel files clean.
4. **Draft-only safety layer** — Hard-blocked from any Send/Apply/Submit/Connect action. You review, you send.
5. **Shortlist-first lead generation** — Contacts are found only for jobs scoring above a threshold, saving API costs and avoiding LinkedIn rate limits.

This architecture aligns with proven patterns in open-source job automation projects like [sentient-engineering/jobber](https://github.com/sentient-engineering/jobber) (browser-controlled AI agent) and [SUSINDRAREDDY/Job-Agent](https://github.com/SUSINDRAREDDY/Job-Agent) (LangGraph multi-agent orchestration), while extending them into a full 5-agent pipeline.

---

## Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| **Orchestration** | LangGraph or sequential Python pipeline | Manages state, routing, and agent handoffs |
| **Browser Automation** | Playwright (`launch_persistent_context`) | Maintains cookies/sessions across runs [RayeesYousufGenAi/multi-platform-job-apply-bot](https://github.com/RayeesYousufGenAi/multi-platform-job-apply-bot) |
| **LLM Intelligence** | GPT-4o-mini or Claude Haiku (cost-efficient) | Parsing, matching, drafting |
| **Data Persistence** | SQLite (internal) + openpyxl (output) | Reliable dedup + clean Excel export |
| **Trigger Interface** | python-telegram-bot library | Commands, auth, progress updates, file delivery |
| **Resume Parsing** | PyPDF2 / pdfplumber | One-time text extraction |
| **Anti-Detection** | `--disable-blink-features=AutomationControlled`, randomized delays | Reduces bot-flagging risk |

---

## Project Structure

```
job-hunt-agent/
├── config.yaml                      # All settings in one file
├── requirements.txt
├── setup_browser.py                 # One-time login to all portals
├── main.py                          # Telegram bot entry point
├── orchestrator.py                  # Pipeline coordinator
├── agents/
│   ├── resume_profile.py            # Agent 0: Parse & cache resume (runs once)
│   ├── sourcing.py                  # Agent 1: Discover job links
│   ├── parsing.py                   # Agent 2: Extract full JDs
│   ├── matching.py                  # Agent 3: Score & rank
│   ├── leadgen.py                   # Agent 4: Find LinkedIn contacts
│   └── messaging.py                 # Agent 5: Draft outreach messages
├── portals/
│   ├── linkedin_jobs.py             # LinkedIn Jobs scraper
│   ├── naukri.py                    # Naukri.com scraper
│   ├── indeed.py                    # Indeed India scraper
│   ├── instahyre.py                 # Instahyre dashboard scraper
│   ├── hirist.py                    # Hirist.tech scraper
│   └── whatsapp_groups.py           # WhatsApp Web group monitor
├── utils/
│   ├── llm.py                       # Async OpenAI wrapper
│   ├── resume_parser.py             # PDF text extraction
│   ├── excel_manager.py             # Single-writer Excel handler
│   └── db.py                        # SQLite dedup & state management
└── data/
    ├── resume.pdf                   # Your resume
    ├── candidate_profile.json       # Cached structured resume
    ├── job_hunt.db                  # SQLite database (source of truth)
    ├── browser_profile/             # Persistent browser sessions
    ├── seen_jobs.json               # De-duplication tracker
    └── output/                      # Daily Excel files
        └── job_hunt_YYYY-MM-DD.xlsx
```

---

## Configuration (`config.yaml`)

```yaml
# ════════════════════════════════════════════════
#  JOB HUNT AGENT – MASTER CONFIGURATION
# ════════════════════════════════════════════════

telegram:
  bot_token: "YOUR_BOT_TOKEN"           # From @BotFather
  allowed_user_ids:
    - 123456789                          # Your numeric ID (@userinfobot)

llm:
  api_key: "sk-YOUR_OPENAI_API_KEY"
  model: "gpt-4o-mini"                   # ~$0.03 per 50-job run
  temperature: 0.3

browser:
  profile_dir: "./data/browser_profile"
  headless: false                         # Visible = lower detection risk
  slow_mo: 800                            # ms between actions

search:
  keywords:
    - "Python Developer"
    - "Backend Engineer"
    - "FastAPI Developer"
  location: "India"
  remote_ok: true
  experience_years: 5
  max_results_per_portal: 25

portals:
  linkedin:
    enabled: true
    base_url: "https://www.linkedin.com/jobs/search/"
  naukri:
    enabled: true
    base_url: "https://www.naukri.com"
  indeed:
    enabled: true
    base_url: "https://www.indeed.co.in"
  instahyre:
    enabled: true
    base_url: "https://www.instahyre.com/candidate/opportunities/"
  hirist:
    enabled: true
    base_url: "https://www.hirist.tech"

whatsapp:
  enabled: true
  group_names:
    - "Python Job Alerts"
    - "Tech Jobs India"
    - "Remote Dev Jobs"
  scan_hours: 24

matching:
  shortlist_threshold: 70                # Only lead-gen for scores ≥ this
  max_shortlist_per_day: 20              # Cap contacts lookups
  weights:
    skills: 0.60                          # Technical skill overlap
    experience: 0.25                     # Years of experience fit
    location: 0.15                       # Remote/city compatibility
  mandatory_skill_cap: 65                # Max score if ≥2 mandatory skills missing

lead_gen:
  contacts_per_job: 3
  search_roles:
    - "Recruiter"
    - "Talent Acquisition"
    - "Engineering Manager"
    - "HR"

resume:
  path: "./data/resume.pdf"

output:
  db_path: "./data/job_hunt.db"
  excel_dir: "./data/output"
  seen_jobs_path: "./data/seen_jobs.json"
```

---

## Browser Session Strategy (Two Options)

### Option A: Dedicated Persistent Profile (Recommended)

Create a separate Chrome/Edge profile called "Job Hunt." Log in to every portal once. All future runs reuse those cookies automatically.

```python
# setup_browser.py — Run this ONCE to log in everywhere
import asyncio
from playwright.async_api import async_playwright

PORTALS = [
    ("LinkedIn",    "https://www.linkedin.com/login"),
    ("Naukri",      "https://www.naukri.com/nlogin/login"),
    ("Indeed",      "https://secure.indeed.com/account/login"),
    ("Instahyre",   "https://www.instahyre.com/login/"),
    ("Hirist",      "https://www.hirist.tech/login"),
    ("WhatsApp",    "https://web.whatsapp.com"),
]

async def setup():
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir="./data/browser_profile",
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = await ctx.new_page()
        for name, url in PORTALS:
            print(f"\n→ Log in to {name}: {url}")
            await page.goto(url)
            input("  Press ENTER after logging in...")
        print("\n✅ All sessions saved.")
        await ctx.close()

asyncio.run(setup())
```

### Option B: Chrome Extension Bridge (For Your Existing Browser)

If you must use your already-open daily browser, build a lightweight Chromium extension that injects content scripts into target portals, communicates via native messaging to a local Python orchestrator, and executes commands within your active session context. This avoids Playwright's context isolation but requires extension development overhead.

---

## The Five Agents (Detailed Design)

---

### Agent 0: Resume Profile Agent (One-Time Setup)

Runs when you first upload your resume or when you explicitly refresh it. Produces a structured JSON that every other agent references — no repeated PDF parsing.

**Input:** Resume PDF  
**Output:** `candidate_profile.json`

```json
{
  "candidate_name": "Your Name",
  "target_roles": ["Backend Engineer", "Python Developer"],
  "total_experience_years": 5,
  "skills": ["Python", "FastAPI", "PostgreSQL", "Docker", "AWS", "Redis"],
  "skill_years": {"Python": 5, "FastAPI": 3, "PostgreSQL": 4, "Docker": 3},
  "locations": ["Bengaluru", "Remote"],
  "projects": [
    {
      "name": "Real-time Analytics Pipeline",
      "summary": "Built Kafka + Spark + S3 event pipeline processing 50K events/sec",
      "skills": ["Kafka", "Spark", "AWS", "Python"]
    }
  ],
  "companies": ["Previous Corp", "Current Inc."]
}
```

**Implementation:** Extract text with PyPDF2, send to LLM with a structuring prompt, save result. Subsequent agents load this file instead of re-reading the PDF.

---

### Agent 1: Sourcing Agent

Visits all enabled portals and monitored WhatsApp groups. Collects job links with metadata. Deduplicates against both the SQLite database and `seen_jobs.json`.

**Portal Adapters:** Each portal module exposes a standard interface:

```python
async def scrape(page, keywords, location, max_results) -> list[dict]:
    """Returns list of {source, title, company, location, url}"""
```

**Robustness Strategy (Dual Extraction):** Every scraper implements two layers:
1. **Primary:** CSS selector-based card extraction (fast, precise while DOM is stable)
2. **Fallback:** Regex URL extraction from raw HTML (always works, even if site redesigns break selectors)

If CSS returns nothing, regex catches every `linkedin.com/jobs/view/...`, `naukri.com/job-listings-...`, `indeed.co.in/viewjob?jk=...`, etc. The Parsing Agent fills in missing metadata later.

**WhatsApp Integration:**

```python
# Pseudocode for WhatsApp group scanning
async def scrape_whatsapp(page, group_names, scan_hours):
    await page.goto("https://web.whatsapp.com")
    
    for group in group_names:
        # Search and open group
        await search_and_open_group(page, group)
        
        # Scroll up to load historical messages
        for _ in range(5):
            await scroll_up_message_pane(page)
        
        # Extract all message text
        messages = await extract_messages(page, time_window=scan_hours)
        
        # Filter URLs that match known job domains
        job_urls = filter_job_urls(messages)  # linkedin.com/jobs, naukri.com, indeed.co, etc.
        
        yield {"source": f"WhatsApp: {group}", "url": url} for url in job_urls
    
    # Deduplicate globally
    return unique_by_url(all_results)
```

**De-duplication Logic:**

Before adding any job to the queue, compute a fingerprint:
```python
def job_fingerprint(job):
    return hashlib.md5(
        f"{normalize(job['company'])}|{normalize(job['title'])}"
        f"|{normalize(job['location'])}|{job['url']}".encode()
    ).hexdigest()
```
Check both `seen_jobs.json` (cross-day persistence) and today's SQLite run table (intra-run duplicates). If a duplicate exists across portals, merge sources: `"LinkedIn; Indeed"`.

**Output:** De-duplicated list of new job stubs written to SQLite `raw_leads` table.

---

### Agent 2: Parsing Agent

Visits each unique job URL extracted by the Sourcing Agent. Converts messy HTML pages into structured records using a three-step fallback chain:

| Step | Method | When Used |
|------|--------|-----------|
| 1 | Site-specific DOM selectors (`.description__text`, `.jobDescriptionContent`, etc.) | Primary path |
| 2 | Generic visible-text extraction from main content area | Selectors fail |
| 3 | LLM structuring from raw page text | Fields incomplete after steps 1–2 |

**LLM Prompt for Step 3:**

> You are a job description parser. Given the raw text of a job listing page, extract valid JSON:
> ```json
> {
>   "title": "...",
>   "company": "...",
>   "location": "...",
>   "remote": "Yes/No/Hybrid",
>   "experience_required": "e.g. 3-5 years",
>   "key_skills": ["skill1", "skill2"],
>   "full_description": "...max 2000 chars..."
> }
> ```
> Return ONLY valid JSON.

**Stored Output per job:** Raw page text, structured JSON, parsing confidence score, screenshot URL on failure. Written to SQLite `parsed_jobs` table.

---

### Agent 3: Matching & Ranking Agent

This is where we combine **deterministic scoring** (for auditability and consistency) with **LLM qualitative analysis** (for nuanced skill matching).

#### Scoring Formula (Deterministic Core)

```
Percentage Match = 100 × (
    0.60 × Skill_Overlap +
    0.25 × Experience_Fit +
    0.15 × Location_Fit
)
```

**A. Skill Overlap (60% weight)**
- Canonicalize synonyms: `Node` → `Node.js`, `JS` → `JavaScript`, `Postgres` → `PostgreSQL`
- Separate required vs. preferred skills
- Weight required at 75%, preferred at 25%
- Example: If JD requires `[Python, FastAPI, Kubernetes]` and you have `[Python, FastAPI, Docker]`: required match = 2/3 = 66.7%

**B. Experience Fit (25% weight)**
| Your Years vs. Required | Score |
|---|---|
| Meets or exceeds | 1.0 |
| Short by 1 year | 0.75 |
| Short by 2 years | 0.45 |
| Short by >2 years | 0.10 |

**C. Location Fit (15% weight)**
| Condition | Score |
|---|---|
| Remote OK + you allow remote | 1.0 |
| Exact city match | 1.0 |
| Hybrid in target city | 0.7 |
| Nearby city / relocation OK | 0.5 |
| Incompatible | 0.0 |

**Mandatory Skill Cap Rule:** If 2 or more mandatory skills are completely missing, ceiling the overall score at 65%, regardless of experience/location fit. This prevents high-scoring but fundamentally unqualified roles from bubbling up.

#### LLM Enhancement Layer (Qualitative)

After computing the formula score, pass the resume highlights + JD to the LLM for:

```json
{
  "matched_projects": ["Project X uses FastAPI+Postgres, directly relevant"],
  "missing_skills_with_guidance": ["Kubernetes – can learn quickly given Docker experience"],
  "match_summary": "Strong backend fit. Missing K8s but solid on core stack.",
  "recommended_priority": "High"
}
```

**Final Output Columns:**
- Match % (formula-derived)
- Skill Score, Experience Score, Location Score (breakdown)
- Matched Skills, Missing Skills
- Match Summary (LLM-generated explanation)
- Recommended Projects (from your resume that align)
- Rank (sorted descending by Match %)

Jobs are sorted and written back to SQLite. Only jobs scoring **≥ threshold (configurable, default 70)** are passed to the next agent.

---

### Agent 4: Lead Generation Agent

**Scope:** Runs **only for shortlisted jobs** (score ≥ threshold OR top N per day). This is critical for cost control and LinkedIn account health — searching people pages is the most rate-limited activity.

**Search Strategy (per company):**

```python
SEARCH_PATTERNS = [
    "{company} Recruiter",
    "{company} Talent Acquisition",
    "{company} Engineering Manager",
    "{company} {job_role_title}",       # Peer in same role
]
```

For each pattern, navigate to:
```
https://www.linkedin.com/search/results/people/?keywords={query}
```
Extract up to 3 unique contacts per job from results cards.

**Contact Record:**
```python
{
    "name": "Jane Doe",
    "title": "Senior Technical Recruiter",
    "linkedin_url": "https://www.linkedin.com/in/janedoe/",
    "relevance_reason": "Recruiter at Acme Corp",
    "confidence": "high"  # Based on title match quality
}
```

**Rate Discipline:**
- 5–8 second delay between company searches
- Limit to 2 search patterns per company (stop after finding 3 contacts)
- If LinkedIn shows blurred results or CAPTCHA, pause this agent and alert via Telegram

**Output:** Contacts written to SQLite `contacts` table, linked by `job_id`.

---

### Agent 5: Messaging Agent

Drafts personalized outreach for each contact. **Zero sending.** Everything goes to Excel for your manual review.

**Per Contact Input:**
- Contact name, title, LinkedIn URL
- Job title, company, tech stack
- Matching skills (from Agent 3)
- Relevant project snippet (from your candidate profile)
- Match score context

**Draft Generation (Two Formats):**

| Format | Length | Content Rules |
|---|---|---|
| **LinkedIn Note** | 250–300 chars | Name, role, 2 skill matches, 1 project mention, polite CTA |
| **Email Draft** | 80–140 words | Subject line, greeting, body with specific details, professional sign-off |

**Example Output:**

> **LinkedIn Note (to Jane Doe @ Acme):**  
> Hi Jane — saw the Senior Python role at Acme. I've spent 5 years building FastAPI microservices and PostgreSQL-backed platforms (including a real-time analytics pipeline processing 50K events/sec). Would love to learn more about the team's stack. Happy to share my resume if useful!

> **Email Subject:** Senior Python Engineer application — [Your Name]  
> **Body:** Hi Jane, I noticed Acme is hiring a Senior Python Engineer and wanted to reach out directly. My background aligns closely with the role — I've spent 5 years working with Python, FastAPI, PostgreSQL, and Docker, most recently building a real-time event processing pipeline on Kafka + Spark that handles 50K events/second. I'd appreciate the opportunity to discuss how my experience could contribute to the team. Best regards, [Your Name]

**Rules enforced:**
- No invented facts or fake familiarity
- No exaggeration of skill levels
- Blank draft if contact name/title is uncertain
- Tone: professional, warm, concise

**Output:** Drafts stored in SQLite `drafts` table, linked by `contact_id`.

---

## Excel Workbook Design

### Sheet 1: `Jobs_Master` (One Row Per Job)

| Column Group | Fields |
|---|---|
| **Identity** | Job ID, Date Found, Source(s), Job Title, Company, Location, Remote?, Job URL |
| **Job Details** | Key Skills Required, Experience Required, Full JD (excerpt, max 1500 chars) |
| **Matching** | **Match %**, Skill Score, Exp Score, Location Score, **Matched Skills**, **Missing Skills**, Match Summary, Rank |
| **Contact 1** | Name, Title, LinkedIn URL, Relevance |
| **Contact 2** | Name, Title, LinkedIn URL, Relevance |
| **Contact 3** | Name, Title, LinkedIn URL, Relevance |
| **Drafts** | LinkedIn Draft (Contact 1), LinkedIn Draft (Contact 2), LinkedIn Draft (Contact 3), Email Draft |
| **Review** | Status (New/Applied/Rejected), Notes |

**Total: ~29 columns per row.** Sorted by Match % descending. Header row frozen. Conditional formatting: green for ≥85%, yellow for 70–84%, red for <70%.

### Sheet 2: `Run_Log`

| Run ID | Start Time | End Time | Jobs Found | Jobs Parsed | Shortlisted | Contacts Found | Drafts Created | Errors |

### Sheet 3: `Config_Snapshot`

Captures the active configuration at run time for reproducibility (keywords, thresholds, portal status).

---

## Single-Writer Excel Pattern

To prevent corruption, **only one component writes to Excel** — the `ExcelManager` utility, called once at the end of the pipeline after all agents complete:

```python
class ExcelManager:
    """Single writer. Called once per run after all agents finish."""
    
    def __init__(self, excel_dir: str):
        self.wb = Workbook()
        self.ws = self.wb.active
        self._write_header()
        self.current_row = 2
    
    def add_job_row(self, job_data: dict, contacts: list, drafts: list):
        """Write one fully-enriched row. Thread-safe by design."""
        # Flatten contacts (×3) and drafts into columns
        row = self._flatten(job_data, contacts, drafts)
        for col_idx, value in enumerate(row, 1):
            cell = self.ws.cell(row=self.current_row, column=col_idx, value=value)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        self.current_row += 1
    
    def save(self) -> Path:
        filepath = Path(self.excel_dir) / f"job_hunt_{date.today()}.xlsx"
        self.wb.save(filepath)
        return filepath
```

---

## Daily Execution Flow (Pseudocode)

```python
async def run_pipeline(config):
    # 0. Load candidate profile (cached from resume)
    profile = load_or_build_profile(config["resume"]["path"])
    
    # Launch persistent browser session
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=config["browser"]["profile_dir"],
            headless=config["browser"]["headless"],
            slow_mo=config["browser"]["slow_mo"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        
        # AGENT 1: Source jobs from all portals + WhatsApp
        report("🔍 Agent 1/5: Sourcing...")
        sourcing = SourcingAgent(config, ctx, db)
        raw_jobs = await sourcing.run()
        report(f"   Found {len(raw_jobs)} new jobs")
        
        if not raw_jobs:
            report("⚠️ No new jobs. Creating empty sheet.")
            return create_empty_excel()
        
        # AGENT 2: Parse each job URL
        report("📄 Agent 2/5: Parsing descriptions...")
        parsing = ParsingAgent(config, ctx, llm)
        parsed_jobs = await parsing.run(raw_jobs)
        
        # AGENT 3: Match & rank against resume
        report("🎯 Agent 3/5: Scoring & ranking...")
        matching = MatchingAgent(config, llm, profile)
        scored_jobs = await matching.run(parsed_jobs)
        top_score = scored_jobs[0]["match_score"] if scored_jobs else 0
        report(f"   Top match: {top_score}%")
        
        # AGENT 4: Find contacts for shortlisted jobs only
        shortlisted = [j for j in scored_jobs 
                       if j["match_score"] >= config["matching"]["shortlist_threshold"]]
        shortlisted = shortlisted[:config["matching"]["max_shortlist_per_day"]]
        
        report(f"👤 Agent 4/5: Finding contacts for {len(shortlisted)} shortlisted jobs...")
        leadgen = LeadGenAgent(config, ctx, llm)
        jobs_with_contacts = await leadgen.run(shortlisted)
        total_contacts = sum(len(j["contacts"]) for j in jobs_with_contacts)
        report(f"   Found {total_contacts} contacts")
        
        # AGENT 5: Draft messages
        report("✉️ Agent 5/5: Drafting outreach...")
        messaging = MessagingAgent(config, llm, profile)
        final_jobs = await messaging.run(jobs_with_contacts)
        
        # WRITE EXCEL (single writer, single call)
        report("📊 Writing Excel sheet...")
        em = ExcelManager(config["output"]["excel_dir"])
        for job in final_jobs:
            em.add_job_row(
                job_data=job,
                contacts=job.get("contacts", []),
                drafts=job.get("drafts", []),
            )
        output_path = em.save()
        
        # LOG RUN METRICS
        log_run(jobs_found=len(raw_jobs), jobs_parsed=len(parsed_jobs),
                shortlisted=len(shortlisted), contacts=total_contacts)
        
        report(f"✅ Complete! → {output_path.name}")
        return output_path
```

---

## Telegram Bot Interface

```python
# Commands available to authorized users only:

/hunt              → Start the full 5-agent pipeline
/status            → Check if a pipeline is currently running
/set_keywords X Y  → Update search keywords (persisted to config)
/config_show       → Display current configuration
/help              → List all commands
```

During execution, the bot sends **live progress messages** to your chat:
```
🚀 Starting daily job hunt…
🔍 Agent 1/5: Sourcing jobs from all portals...
   Found 47 new jobs
📄 Agent 2/5: Extracting job descriptions...
   Parsed 47 JDs
🎯 Agent 3/5: Matching & ranking against your resume...
   Top match: 92%
👤 Agent 4/5: Finding LinkedIn contacts for 18 shortlisted jobs...
   Found 42 contacts
✉️ Agent 5/5: Drafting outreach messages...
   Drafts ready
📊 Writing Excel sheet...
✅ Done! 47 jobs processed → job_hunt_2026-04-07.xlsx
[Excel file attached]
```

After completion, the bot **delivers the Excel file directly** to your Telegram chat for immediate download and review.

---

## Operational Guardrails & Safety Mechanisms

| Risk | Mitigation |
|---|---|
| **Auto-send prevention** | Blacklist action words: `Send`, `Apply`, `Submit`, `Connect`, `Post`. Block any DOM interaction matching these patterns. |
| **CAPTCHA / human verification** | Detect challenge dialogs via screenshot analysis. Pause affected adapter, send Telegram alert with screenshot, continue other portals. |
| **WhatsApp session expiry** | Check for QR code prompt on page load. If detected, alert: *"WhatsApp session expired. Please re-scan QR code in the browser window."* Pause WA adapter only. |
| **LinkedIn rate limiting** | Aggressive delays (5–8s between searches). Shortlist-only contact queries. Stop on HTTP 429/999 responses. Alert via Telegram. |
| **Account lockout risk** | Human-like typing delays, random scroll patterns, visible browser mode (`headless=False`), dedicated profile separation from personal browsing. |
| **Excel performance** | Truncate long texts (JD excerpts at 1500 chars, drafts at 500 chars). One write per run. Separate sheets for logs vs. data. |
| **Duplicate processing** | Dual dedup: URL-based (exact matches) + fingerprint-based (cross-portal same role). SQLite `seen_jobs` table persists across days. |
| **WhatsApp privacy scope** | Only scan whitelisted groups. Only extract URLs and job-related text. Never store phone numbers or personal chat content outside the job link context. |
| **Local-first data** | Cookies, sessions, resume, database, and output files remain on your machine. Nothing uploaded to third-party servers except LLM API calls (text-only, no credentials). |

---

## Cost Estimate (Per Daily Run)

| Component | Tokens (est.) | Cost (GPT-4o-mini) |
|---|---|---|
| Resume profiling (one-time) | ~3K input | $0.0005 |
| JD parsing × 50 jobs | ~150K input + 50K output | $0.055 |
| Matching × 50 jobs | ~200K input + 25K output | $0.068 |
| Message drafting × 40 contacts | ~80K input + 30K output | $0.033 |
| **Daily total** | **~485K tokens** | **~$0.15/day** |

At roughly **$4.50/month**, this system is cost-effective compared to the hours saved manually searching, reading, cross-referencing, and drafting.

---

## Setup & First Run

```bash
# 1. Clone and install dependencies
git clone <your-repo>
cd job-hunt-agent
pip install -r requirements.txt
playwright install chromium

# 2. Configure
cp config.yaml.example config.yaml
# Edit with: Telegram bot token, user ID, OpenAI key, search keywords

# 3. Place your resume
cp ~/Downloads/my_resume.pdf ./data/resume.pdf

# 4. One-time browser setup (log in to every portal)
python setup_browser.py
# Follow prompts to log into LinkedIn, Naukri, Indeed, 
# Instahyre, Hirist, and WhatsApp Web

# 5. Build your candidate profile (runs once)
python -c "from agents.resume_profile import build_profile; build_profile()"

# 6. Start the Telegram bot
python main.py

# 7. Open Telegram → find your bot → send /hunt
# Watch progress, receive Excel when done
```

---

## Reference Implementations & Validation

This design draws on patterns validated by several open-source job automation projects:

- **[sentient-engineering/jobber](https://github.com/sentient-engineering/jobber)** — Demonstrates browser-controlled AI agents applying autonomously, using persistent Chrome profiles and multi-agent conversation patterns between planners and browser controllers.
- **[RayeesYoursufGenAi/multi-platform-job-apply-bot](https://github.com/RayeesYousufGenAi/multi-platform-job-apply-bot)** — Validates Selenium/Playwright-based multi-platform automation across LinkedIn, Indeed, Naukri, Glassdoor with anti-detection measures and human-like typing delays.
- **[SUSINDRAREDDY/Job-Agent](https://github.com/SUSINDRAREDDY/Job-Agent)** — Confirms LangGraph multi-agent orchestration with specialized subagents (Browser Agent for navigation, Apply Agent for forms) using Playwright for reliable automation.
- **[sreekar2858/JobSearch-Agent](https://github.com/sreekar2858/JobSearch-Agent)** — Provides reference implementation for LinkedIn scraping with anonymization, proxy support, and AI-powered CV/cover letter generation.
- **[vivek7557/Ai-Job-hunting-Agent](https://github.com/vivek7557/Ai-Job-hunting-Agent)** — Validates TF-IDF + cosine similarity matching approach and Streamlit-based dashboards for job ranking visualization.

Your system synthesizes these proven approaches into a unified 5-agent pipeline with strict human-in-the-loop control, deterministic scoring for auditability, and production-ready error handling.

---

## Summary

This system gives you a **lightweight, efficient pipeline** that:

1. ✅ Searches **6 sources** daily (5 portals + WhatsApp groups) within your logged-in browser
2. ✅ Parses and structures every job description intelligently
3. ✅ Scores and ranks using an **auditable formula** (not black-box LLM guesses)
4. ✅ Finds **real LinkedIn contacts** at each company (recruiters, hiring managers, peers)
5. ✅ Drafts **personalized messages** mentioning specific skills, projects, and names
6. ✅ Delivers everything in a **single organized Excel file** via Telegram
7. ✅ **Never sends anything automatically** — you review, you decide, you connect

The entire process triggers with one `/hunt` message and costs under $0.20 per day.
