"""Parsing Agent — visits job URLs and extracts structured JD details.

For each job with parse_status='pending', visits the URL and extracts:
- Full job description text
- Structured fields (title, company, location, remote, experience, skills)
- Apply link (Greenhouse, Lever, Workday, company career page)
- JD summary

Uses a 3-step fallback: site-specific selectors → generic extraction → LLM structuring.
"""

import asyncio
import json
import re
from typing import Any, Optional

from playwright.async_api import BrowserContext, Page

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.browser import human_delay, new_page, safe_goto
from src.core.db import Database
from src.core.llm import LLMClient
from src.core.roles import classify_role_family, is_placeholder_text

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
  "experience_required": "Exactly one of: 0-2 years, 3-5 years, 5-8 years, 8-12 years, 12+ years",
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
        self.delay_between_calls = config.get("llm", {}).get("delay_between_calls_s", 3)

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

        for i, job in enumerate(pending_jobs):
            try:
                result = await self._parse_job(page, job)
                if result:
                    self.db.update_job(job["id"], **result, parse_status="parsed")
                    parsed_count += 1
                    logger.info("parsing_job_done", progress=f"{i+1}/{len(pending_jobs)}",
                                title=job.get("title", "")[:50], company=job.get("company", ""))
                else:
                    self.db.update_job(job["id"], parse_status="failed")
                    errors.append(f"No content: {job['url']}")
            except Exception as e:
                self.db.update_job(job["id"], parse_status="failed")
                errors.append(f"{job['url']}: {str(e)}")
                logger.warning("parsing_job_failed", url=job["url"], error=str(e))

            # Delay between jobs to avoid rate limits
            await asyncio.sleep(self.delay_between_calls)
            await human_delay(1, 2)

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

        # 2. Extract apply link (external ATS / company career page)
        apply_url = await self._extract_apply_link(page, url)

        # 3. Send to LLM for structuring
        try:
            structured = await self.llm.complete_json(
                prompt=f"Parse this job description:\n\n{jd_text[:4000]}",
                system=JD_PARSE_SYSTEM,
                agent=self.name,
            )
        except Exception as e:
            logger.warning("parsing_llm_failed", url=url, error=str(e))
            # Fall back to raw text storage — still save apply link + JD
            fallback = {
                "full_description": jd_text[:5000],
                "jd_summary": jd_text[:300],
                "required_skills": [],
                "preferred_skills": [],
                "skills_required": [],
                "role_family_hint": classify_role_family(job.get("title", ""), jd_text[:500]),
            }
            if apply_url:
                fallback["apply_url"] = apply_url
            return fallback

        required_skills = self._clean_skills(structured.get("skills_required", []))
        preferred_skills = self._clean_skills(structured.get("skills_preferred", []))
        combined_skills = self._merge_skill_lists(required_skills, preferred_skills)
        derived_title = structured.get("title") or job.get("title", "")
        role_family_hint = classify_role_family(derived_title, jd_text[:1000])

        # Build update fields
        updates = {
            "full_description": jd_text[:5000],
            "jd_summary": structured.get("summary", ""),
            "required_skills": required_skills,
            "preferred_skills": preferred_skills,
            "skills_required": combined_skills,
            "role_family_hint": role_family_hint,
        }

        # Backfill weak source metadata, but preserve good scraped values.
        if self._should_backfill_field(job.get("title")) and structured.get("title"):
            updates["title"] = structured["title"]
        if self._should_backfill_field(job.get("company")) and structured.get("company"):
            updates["company"] = structured["company"]
        if self._should_backfill_field(job.get("location")) and structured.get("location"):
            updates["location"] = structured["location"]
        if not job.get("remote") and structured.get("remote"):
            updates["remote"] = structured["remote"]
        if not job.get("experience_required") and structured.get("experience_required"):
            updates["experience_required"] = structured["experience_required"]
        if apply_url:
            updates["apply_url"] = apply_url

        return updates

    @staticmethod
    def _clean_skills(skills: list[str]) -> list[str]:
        cleaned = []
        seen = set()
        for skill in skills or []:
            normalized = re.sub(r"\s+", " ", (skill or "").strip())
            if normalized and normalized.lower() not in seen:
                seen.add(normalized.lower())
                cleaned.append(normalized)
        return cleaned

    @staticmethod
    def _merge_skill_lists(required: list[str], preferred: list[str]) -> list[str]:
        merged = []
        seen = set()
        for skill in list(required) + list(preferred):
            key = skill.lower()
            if key not in seen:
                seen.add(key)
                merged.append(skill)
        return merged

    @staticmethod
    def _should_backfill_field(existing_value: str) -> bool:
        return is_placeholder_text(existing_value)

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

    async def _extract_apply_link(self, page: Page, job_url: str) -> str:
        """Extract external apply link (ATS/company career page).

        Strategy:
        1. Look for direct ATS links in the page HTML
        2. Look for LinkedIn/Naukri "Apply" buttons that link externally
        3. Scan all page links for known ATS domains
        4. Check if any link goes to a company careers page
        """
        # 1. Direct ATS link selectors
        for selector in APPLY_SELECTORS:
            try:
                el = await page.query_selector(selector)
                if el:
                    href = await el.get_attribute("href")
                    if href and any(domain in href for domain in ATS_DOMAINS):
                        return href
            except Exception:
                continue

        # 2. LinkedIn-specific: external apply button
        if "linkedin.com" in job_url:
            try:
                # LinkedIn shows "Apply" for external jobs (not "Easy Apply")
                apply_btn = await page.query_selector(
                    "button.jobs-apply-button, a.jobs-apply-button, "
                    "button[aria-label*='Apply'], a[aria-label*='Apply']"
                )
                if apply_btn:
                    btn_text = (await apply_btn.inner_text()).strip().lower()
                    # "Apply" (external) vs "Easy Apply" (internal)
                    if "easy" not in btn_text:
                        # Click to see where it goes — capture the popup/redirect URL
                        try:
                            async with page.expect_popup(timeout=5000) as popup_info:
                                await apply_btn.click()
                            popup = await popup_info.value
                            external_url = popup.url
                            await popup.close()
                            if external_url and external_url != job_url:
                                logger.info("apply_link_found_via_popup", url=external_url[:80])
                                return external_url
                        except Exception:
                            pass  # No popup, might be a redirect
            except Exception:
                pass

        # 3. Naukri-specific: apply button
        if "naukri.com" in job_url:
            try:
                apply_btn = await page.query_selector(
                    "a#apply-button, a.apply-btn, button#apply-button, "
                    "a[class*='applyButton']"
                )
                if apply_btn:
                    href = await apply_btn.get_attribute("href")
                    if href and not href.startswith("javascript"):
                        return href
            except Exception:
                pass

        # 4. Scan all links for ATS domains or company career pages
        try:
            links = await page.query_selector_all("a[href]")
            for link in links[:80]:
                href = await link.get_attribute("href")
                if not href:
                    continue
                # ATS domains
                if any(domain in href for domain in ATS_DOMAINS):
                    return href
                # Company career pages
                if "/careers/" in href or "/jobs/" in href:
                    link_text = (await link.inner_text()).strip().lower()
                    if any(w in link_text for w in ["apply", "career", "position"]):
                        return href
        except Exception:
            pass

        return ""
