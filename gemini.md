Designing a multi-agent system for this workflow is a highly effective way to streamline your job search. However, executing this entirely through browser automation on your logged-in personal accounts carries a high risk. Platforms like LinkedIn and Naukri have aggressive anti-bot mechanisms. Automating actions on your primary accounts, even without sending messages, can lead to shadowbans or permanent account suspension.

To make this system viable and protect your accounts, you must implement strict rate limiting, randomized human-like delays, and use stealth browser profiles.

Here is the architectural design and workflow for your multi-agent job hunting system.

### System Architecture

  * **Orchestrator:** Python script triggered via a Telegram Bot API (`python-telegram-bot`).
  * **Agent Framework:** CrewAI or LangGraph to manage the hand-offs between specialized agents.
  * **Browser Automation:** Playwright (Python) using the `playwright-stealth` plugin and connecting to your existing local Chrome profile using the `user-data-dir` flag. This maintains your logged-in sessions.
  * **Data Management:** `pandas` and `openpyxl` for reading, updating, and formatting the central Excel tracking sheet.
  * **Intelligence:** An LLM API (like Gemini) to power the matching, ranking, and drafting agents.

-----

### Agent Workflow and Responsibilities

#### 1\. Sourcing Agent

  * **Role:** Scour predefined search URLs (LinkedIn Jobs, Naukri, Instahyre, Hirist) and read recent messages in pinned WhatsApp Web groups.
  * **Action:** Navigates to the saved search URLs, scrapes the job posting URLs from the results feed, and extracts links shared in WhatsApp groups.
  * **Output:** Creates the initial rows in the Excel sheet with the `Platform`, `Job Title`, and `Job URL`.

#### 2\. Parsing Agent

  * **Role:** Extract the raw data from the sourced links.
  * **Action:** Iterates through the newly added URLs in the Excel sheet. It navigates to each page, waits for the DOM to load, and extracts the full text of the Job Description (JD), Company Name, Location, and Work Mode (Remote/Hybrid/On-site).
  * **Output:** Populates the `Company`, `Location`, `Work Mode`, and `Raw JD` columns in the Excel sheet.

#### 3\. Matching & Ranking Agent

  * **Role:** Evaluate your fit for the role.
  * **Action:** Reads your pre-loaded Resume (parsed using `pdfplumber`). It feeds both your resume text and the `Raw JD` into an LLM prompt designed to evaluate three criteria:
      * *Technical Skills Overlap*
      * *Experience Alignment*
      * *Location/Mode Fit*
  * **Output:** Calculates a combined `Percentage Match` score and writes a brief 1-2 sentence `Match Rationale`. Jobs scoring below a defined threshold (e.g., 60%) are flagged to be hidden or skipped in the next steps.

#### 4\. Lead Generation Agent

  * **Role:** Identify the right people to contact.
  * **Action:** For jobs that pass the matching threshold, this agent navigates to the company's LinkedIn "People" page. It searches using keywords like "Talent Acquisition," "Technical Recruiter," or "[Your Target Role] Manager."
  * **Output:** Scrapes the names, titles, and LinkedIn profile URLs of 2-3 relevant individuals. It formats these into a single cell (e.g., as a JSON string or line-separated text) to maintain the "one row per job" requirement.

#### 5\. Messaging Agent

  * **Role:** Prepare personalized outreach drafts.
  * **Action:** Takes the output from the Matching Agent (the specific overlapping skills and relevant projects) and the names from the Lead Generation Agent. It uses an LLM to generate concise, highly customized connection request notes (under 300 characters) and slightly longer email drafts.
  * **Output:** Populates the `Connection Drafts` and `Email Drafts` columns in the Excel sheet.

-----

### Central Excel Sheet Structure

The state of the system is maintained in this table.

| Platform | Job Title | Company | Job URL | Match Score | Rationale | Potential Contacts (Name - Title - URL) | Connection Drafts | Email Draft |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Instahyre | Backend Eng | TechCorp | [Link] | 88% | Strong Python/SQL overlap. | 1. Jane Doe - Recruiter - [Link]<br>2. John Smith - Eng Manager - [Link] | Hi Jane, saw the Backend role... | Hi Jane, I am reaching out... |
| LinkedIn | SDE II | FinTech Inc | [Link] | 75% | Good exp, lacks AWS. | 1. Alex Ray - TA - [Link] | Hi Alex, noticed the SDE II... | Hi Alex, I am writing to... |

-----

### Execution Trigger

1.  You send a command (e.g., `/hunt`) to your private Telegram bot.
2.  The webhook triggers the master Python script on your local machine (or a local server where your browser session lives).
3.  The orchestrator initializes Playwright, opens the browser instance with your existing cookies, and sequentially runs the Sourcing, Parsing, Matching, Lead Gen, and Messaging agents.
4.  Once complete, the bot replies on Telegram with a summary: *"Job hunt complete. 45 links sourced, 12 high-match jobs processed. Excel sheet updated."*

How would you like to handle the technical implementation of the Playwright stealth profile to minimize the risk of your LinkedIn session being flagged during the Lead Generation phase?
