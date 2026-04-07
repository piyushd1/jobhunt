Here is a clean design for a **browser-first, human-reviewed job-hunt automation system** that does exactly what you described: discover jobs, extract details, rank them against your resume, find contacts, and draft outreach — while keeping **all sending manual**.

## 1) System shape

Use a **central orchestrator** with 6 specialist agents plus a browser runner:

**Telegram Trigger → Orchestrator → Browser Runner → Agents → Excel Workbook**

The agents:

1. **Sourcing Agent**
   Finds job links from LinkedIn, Naukri, Indeed, Instahyre, Hirist, and your monitored WhatsApp groups.

2. **Parsing Agent**
   Opens each link in the browser and extracts the full job description and structured fields.

3. **Matching & Ranking Agent**
   Reads your resume PDF, compares it to the job description, and computes a percentage match score.

4. **Lead Generation Agent**
   Finds likely recruiters/peers for that job on LinkedIn and stores multiple contacts per role.

5. **Messaging Agent**
   Drafts personalized LinkedIn and email outreach for each contact, but never sends anything.

6. **QA / Dedup / Sheet Writer Agent**
   Cleans duplicates, checks missing fields, and writes everything into Excel.

A **browser runner** sits underneath all of them and performs actions inside your logged-in browser session, so your identity and session stay intact.

---

## 2) Trigger and control flow

### Daily trigger

You send a Telegram command like:

`/hunt start`

That triggers the orchestrator, which then:

1. Opens your persistent browser profile
2. Loads the target portals and WhatsApp Web
3. Collects new job links
4. Processes them end-to-end
5. Writes everything to Excel
6. Returns a completion summary on Telegram

### Human-in-the-loop behavior

The system should stop short of outreach. It should only prepare:

* job list
* ranking
* contact list
* draft message
* draft email

You review and send manually.

---

## 3) Excel workbook design

Use one workbook with multiple tabs. The main tab should be **one row per job**, as you requested.

### Main sheet: `Jobs`

Recommended columns:

* Job ID
* Source
* Source Link
* Company
* Job Title
* Location
* Remote / Hybrid / On-site
* Experience Required
* JD Summary
* Full JD Text
* Percentage Match
* Match Reasons
* Missing Skills
* Strong Skill Matches
* Priority Tier
* Contact 1 Name
* Contact 1 Role
* Contact 1 LinkedIn URL
* Contact 1 Draft DM
* Contact 1 Draft Email
* Contact 2 Name
* Contact 2 Role
* Contact 2 LinkedIn URL
* Contact 2 Draft DM
* Contact 2 Draft Email
* Contact 3 Name
* Contact 3 Role
* Contact 3 LinkedIn URL
* Contact 3 Draft DM
* Contact 3 Draft Email
* Status
* Notes

### Supporting tabs

* **Raw_Sources**: every discovered link, before dedupe
* **Contacts**: normalized contact records, one row per person
* **Resume_Profile**: extracted skills, years of experience, project bank
* **Logs**: errors, captcha stops, skipped pages, confidence issues

That gives you the one-row-per-job view while still preserving detail elsewhere.

---

## 4) Agent responsibilities

### A. Sourcing Agent

This agent searches all source channels.

**What it does**

* Uses browser search on each portal
* Pulls job links from WhatsApp group messages
* Captures title/company/location snippet
* Deduplicates by URL and job title

**Output**

* A list of candidate job URLs with source metadata

**Important behavior**

* It should not log in separately.
* It should always use your already logged-in browser session.
* It should stop when CAPTCHA or unusual login friction appears.

---

### B. Parsing Agent

This agent visits each job link and extracts the full JD.

**What it extracts**

* Job title
* Company
* Location
* Experience
* Skills
* Responsibilities
* Nice-to-haves
* Application link
* Posted date if available

**Implementation idea**
Use page structure first; fall back to readable text extraction only when needed.

**Output**

* Clean structured job record
* Full JD text saved into the workbook

---

### C. Matching & Ranking Agent

This is the scoring engine.

It should compare your resume PDF with the job description in three layers:

#### 1. Skill overlap

Examples:

* exact skill match
* near match
* adjacent match

#### 2. Experience fit

Compare:

* years requested vs your years
* level fit: PM, GPM, senior PM, growth, platform, etc.

#### 3. Location / remote fit

Score higher if:

* same city
* remote-friendly
* hybrid acceptable
* location mismatch gets a penalty

### Suggested scoring formula

Use a transparent score out of 100:

* **Skill overlap: 60%**
* **Experience fit: 25%**
* **Location/remote fit: 15%**

Then apply small penalties for:

* missing mandatory skills
* too-large experience gap
* location mismatch

Example:

`Percentage Match = 0.60 * Skills + 0.25 * Experience + 0.15 * Location`

Also store:

* top matched skills
* missing key skills
* reasoning text

That makes the score explainable instead of being a black box.

---

### D. Lead Generation Agent

For each job, find likely contacts on LinkedIn:

* recruiter
* talent acquisition partner
* hiring manager
* team member in the role

**Search strategy**

* company name + job title
* company name + recruiter
* company name + talent acquisition
* company name + role title

**Output per job**

* up to 3 contacts
* contact name
* role
* LinkedIn URL
* why this contact is relevant

Since you want multiple contacts per row, put them in repeated contact columns in the main sheet and also store them in the `Contacts` tab.

---

### E. Messaging Agent

This agent drafts tailored messages only.

It should use these ingredients:

* recruiter’s name
* job title
* one or two matched technical skills
* one relevant project from your background
* a short, direct ask

### Draft structure

For LinkedIn DM:

* greeting
* role mention
* 1–2 skill matches
* one line on relevant experience
* ask for consideration or referral route

For email:

* subject line
* greeting
* role reference
* concise fit summary
* resume attachment note
* polite close

### Example template

> Hi {Name}, I came across the {Job Title} role at {Company}. My background in {Matched Skill 1} and {Matched Skill 2}, plus my work on {Relevant Project}, looks closely aligned with what the role needs. I’d love to be considered and have attached my resume. Thank you for your time.

This should be saved into Excel, not sent.

---

## 5) Browser-first implementation

To preserve your identity and logged-in sessions, the system should use:

* a **persistent browser profile**
* **session cookies stored locally**
* **Playwright** or a similar browser automation layer
* no separate credential handoff to external APIs for the target sites

This means:

* LinkedIn, Naukri, Indeed, Instahyre, Hirist, and WhatsApp Web all run inside the same browser context
* the system acts like a human browser user
* your account stays in control

For reliability, the browser runner should support:

* screenshot capture
* retry on transient failures
* stop-on-captcha behavior
* page timeouts
* scroll-and-load handling

---

## 6) Recommended architecture

A practical stack would be:

* **Telegram Bot** for trigger
* **Python backend** for orchestration
* **Playwright** for browser control
* **OpenAI model layer** for extraction, matching, and drafting
* **Pandas / openpyxl** for Excel writing
* **SQLite or Postgres** for state and dedupe
* **Scheduled run** via cron or a small job runner

If you want a more agentic approach, use:

* **LangGraph** for workflow state
* or a simple state machine if you want maximum reliability

For this kind of task, a **deterministic workflow with agent steps** is usually better than a fully free-form agent swarm.

---

## 7) Recommended safeguards

A system like this should include:

* **human approval required** before any outreach
* **no auto-send**
* **rate limits** per source
* **captcha and challenge detection**
* **dedupe by URL + title + company**
* **logging of each action**
* **confidence scores** for extracted job data
* **manual override** for any weird page or ambiguous contact

Also, keep in mind that some sites may restrict automation or scraping in their terms. The safest design is to keep the automation lightweight, session-bound, and review-based.

---

## 8) Best operating workflow

Here is the full daily sequence:

1. Telegram trigger starts the run
2. Sourcing Agent gathers fresh job links from portals and WhatsApp groups
3. Parsing Agent opens each job and extracts the JD
4. Matching Agent scores each role against your resume
5. Lead Generation Agent finds contacts
6. Messaging Agent drafts outreach for each contact
7. Sheet Writer saves everything into Excel
8. You review the workbook and manually reach out

---

## 9) Practical enhancements

A few additions will make the system much stronger:

* a **project/story bank** from your resume for better personalization
* a **blacklist** of irrelevant titles/companies
* a **priority rule** for remote roles or target cities
* a **confidence flag** for jobs with incomplete data
* a **daily summary report**: total jobs found, top matches, top contacts, draft count

---

If helpful, I can turn this into a **full technical blueprint** with:

1. agent prompts,
2. Excel column schema,
3. scoring formula,
4. and an implementation roadmap.

