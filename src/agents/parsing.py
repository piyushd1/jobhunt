"""Parsing Agent — visits job URLs and extracts structured JD details.

For each job with parse_status='pending', visits the URL and extracts:
- Full job description text
- Structured fields (title, company, location, remote, experience, skills)
- Apply link (Greenhouse, Lever, Workday, company career page)
- JD summary

Uses a 3-step fallback: site-specific selectors → generic extraction → LLM structuring.
"""

import json
import re
from typing import Any, Optional

from playwright.async_api import BrowserContext, Page

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.browser import human_delay, new_page, safe_goto
from src.core.db import Database
from src.core.llm import LLMClient

logger = structlog.get_logger()

# Known ATS domains for apply link extraction
ATS_DOMAINS = [
    "greenhouse.io", "lever.co", "workday.com", "myworkdayjobs.com",
    "smartrecruiters.com", "ashbyhq.com", "boards.eu.greenhouse.io",
    "jobs.lever.co", "icims.com", "taleo.net", "bamboohr.com",
    "recruitee.com", "breezy.hr", "jazz.co", "applytojob.com",
    "careers.google.com", "jobs.apple.com", "jobs.microsoft.com",
]

# Selectors for JD extraction, ordered by specificity
JD_SELECTORS = {
    "linkedin.com": [
        "div.description__text",
        "div.show-more-less-html__markup",
        "article.jobs-description",
        "div[class*='description']",
    ],
    "naukri.com": [
        "div.job-desc",
        "div.dang-inner-html",
        "section.job-desc",
        "div[class*='job-description']",
    ],
    "foundit.in": [
        "div.job-description",
        "div.card-job-description",
        "div[class*='jobDescription']",
    ],
    "_generic": [
        "div[class*='job-description']",
        "div[class*='jobDescription']",
        "div[class*='description']",
        "section[class*='description']",
        "article",
        "main",
    ],
}

# Selectors for apply button links
APPLY_SELECTORS = [
    "a[href*='greenhouse.io']",
    "a[href*='lever.co']",
    "a[href*='workday.com']",
    "a[href*='myworkdayjobs.com']",
    "a[href*='smartrecruiters.com']",
    "a[href*='ashbyhq.com']",
    "a[href*='icims.com']",
    "a[class*='apply']",
    "button[class*='apply']",
    "a[data-tracking*='apply']",
]

JD_PARSE_SYSTEM = """You are a job description parser for Product Management roles.

Given raw job description text, extract structured data. Return ONLY valid JSON:
{
  "title": "Job Title",
  "company": "Company Name",
  "location": "City, Country or Remote",
  "remote": "Remote / Hybrid / On-site / Unknown",
  "experience_required": "3-5 years or Senior level",
  "skills_required": ["skill1", "skill2", ...],
  "skills_preferred": ["skill3", "skill4", ...],
  "summary": "2-3 sentence summary of the role and key responsibilities"
}

For skills, extract BOTH required and preferred/nice-to-have separately.
Use standard skill names (e.g., "Agile/Scrum" not just "Agile").
If a field cannot be determined, use an empty string or empty list."""


class ParsingAgent(BaseAgent):
    """Visit job URLs and extract structured JD details."""

    name = "parsing"

    def __init__(self, config: dict, db: Database, browser_ctx: BrowserContext, llm: LLMClient):
        super().__init__(config)
        self.db = db
        self.browser_ctx = browser_ctx
        self.llm = llm

    async def run(self, input_data: Any = None) -> AgentResult:
        """Parse all jobs with parse_status='pending'."""
        pending_jobs = self.db.get_jobs(parse_status="pending")
        if not pending_jobs:
            logger.info("parsing_nothing_pending")
            return AgentResult(data=[], count=0)

        logger.info("parsing_start", pending=len(pending_jobs))
        page = await new_page(self.browser_ctx)
        parsed_count = 0
        errors: list[str] = []

        for job in pending_jobs:
            try:
                result = await self._parse_job(page, job)
                if result:
                    self.db.update_job(job["id"], **result, parse_status="parsed")
                    parsed_count += 1
                else:
                    self.db.update_job(job["id"], parse_status="failed")
                    errors.append(f"No content: {job['url']}")
            except Exception as e:
                self.db.update_job(job["id"], parse_status="failed")
                errors.append(f"{job['url']}: {str(e)}")
                logger.warning("parsing_job_failed", url=job["url"], error=str(e))

            await human_delay(2, 4)

        await page.close()
        logger.info("parsing_complete", parsed=parsed_count, failed=len(errors))

        return AgentResult(
            data={"parsed": parsed_count, "failed": len(errors)},
            count=parsed_count,
            errors=errors,
        )

    async def _parse_job(self, page: Page, job: dict) -> Optional[dict]:
        """Parse a single job URL. Returns update fields or None."""
        url = job["url"]
        if not await safe_goto(page, url):
            return None

        await human_delay(1, 2)

        # 1. Extract raw JD text
        jd_text = await self._extract_jd_text(page, url)
        if not jd_text or len(jd_text.strip()) < 50:
            return None

        # 2. Extract apply link
        apply_url = await self._extract_apply_link(page)

        # 3. Send to LLM for structuring
        try:
            structured = await self.llm.complete_json(
                prompt=f"Parse this job description:\n\n{jd_text[:4000]}",
                system=JD_PARSE_SYSTEM,
                agent=self.name,
            )
        except Exception as e:
            logger.warning("parsing_llm_failed", url=url, error=str(e))
            # Fall back to raw text storage
            return {
                "full_description": jd_text[:5000],
                "jd_summary": jd_text[:300],
            }

        # Build update fields
        updates = {
            "full_description": jd_text[:5000],
            "jd_summary": structured.get("summary", ""),
            "skills_required": json.dumps(
                structured.get("skills_required", []) + structured.get("skills_preferred", [])
            ),
        }

        # Only override fields if they were missing from sourcing
        if not job.get("title") and structured.get("title"):
            updates["title"] = structured["title"]
        if not job.get("company") and structured.get("company"):
            updates["company"] = structured["company"]
        if not job.get("location") and structured.get("location"):
            updates["location"] = structured["location"]
        if not job.get("remote") and structured.get("remote"):
            updates["remote"] = structured["remote"]
        if not job.get("experience_required") and structured.get("experience_required"):
            updates["experience_required"] = structured["experience_required"]
        if apply_url:
            updates["apply_url"] = apply_url

        return updates

    async def _extract_jd_text(self, page: Page, url: str) -> str:
        """Extract job description text using site-specific or generic selectors."""
        # Determine which selector set to use
        selectors = []
        for domain, sels in JD_SELECTORS.items():
            if domain != "_generic" and domain in url:
                selectors = sels
                break
        selectors = selectors + JD_SELECTORS["_generic"]

        # Try each selector
        for selector in selectors:
            try:
                el = await page.query_selector(selector)
                if el:
                    text = await el.inner_text()
                    if text and len(text.strip()) > 50:
                        return text.strip()
            except Exception:
                continue

        # Final fallback: get all visible text from body
        try:
            text = await page.inner_text("body")
            if text and len(text.strip()) > 100:
                return text.strip()[:5000]
        except Exception:
            pass

        return ""

    async def _extract_apply_link(self, page: Page) -> str:
        """Extract external apply link (ATS/company career page)."""
        for selector in APPLY_SELECTORS:
            try:
                el = await page.query_selector(selector)
                if el:
                    href = await el.get_attribute("href")
                    if href and any(domain in href for domain in ATS_DOMAINS):
                        return href
            except Exception:
                continue

        # Fallback: scan all links for ATS domains
        try:
            links = await page.query_selector_all("a[href]")
            for link in links[:50]:  # Limit to avoid scanning entire page
                href = await link.get_attribute("href")
                if href and any(domain in href for domain in ATS_DOMAINS):
                    return href
        except Exception:
            pass

        return ""
