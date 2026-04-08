"""Lead Gen Agent — finds relevant contacts at shortlisted companies.

Uses 3 strategies in priority order:
1. LinkedIn Company Page → People tab (only shows CURRENT employees)
2. LinkedIn People Search with boolean operators + current company verification
3. Text/link scanning with headline verification

Rate-limited: max 20 companies/day, 5-8s delay between searches.
"""

import asyncio
import random
import re
import uuid
from typing import Any, Optional
from urllib.parse import quote_plus

from playwright.async_api import BrowserContext, Page

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.browser import human_delay, new_page, safe_goto, random_scroll
from src.core.db import Database

logger = structlog.get_logger()

# Search queries for finding contacts — boolean operators in UPPERCASE
SEARCH_TEMPLATES = [
    "Recruiter OR Talent Acquisition",
    "HR OR Human Resources",
    "Product Manager OR Engineering Manager",
]


class LeadGenAgent(BaseAgent):
    """Find relevant contacts at shortlisted companies via LinkedIn."""

    name = "leadgen"

    def __init__(self, config: dict, db: Database, browser_ctx: BrowserContext):
        super().__init__(config)
        self.db = db
        self.browser_ctx = browser_ctx

        lead_config = config.get("lead_gen", {})
        self.contacts_per_job = lead_config.get("contacts_per_job", 3)
        delay_range = lead_config.get("delay_between_searches_s", [5, 8])
        self.delay_min = delay_range[0] if isinstance(delay_range, list) else 5
        self.delay_max = delay_range[1] if isinstance(delay_range, list) and len(delay_range) > 1 else 8

        match_config = config.get("matching", {})
        self.threshold = match_config.get("shortlist_threshold", 70)
        self.max_per_day = match_config.get("max_shortlist_per_day", 20)

        # Eval metrics
        self._strategy_stats = {
            "company_page": 0, "people_search": 0,
            "text_scan": 0, "failed": 0,
            "skipped_former_employee": 0,
        }

    async def run(self, input_data: Any = None) -> AgentResult:
        scored_jobs = self.db.get_jobs(parse_status="parsed", min_score=self.threshold)

        jobs_to_process = []
        for job in scored_jobs:
            existing = self.db.get_contacts_for_job(job["id"])
            if not existing:
                jobs_to_process.append(job)

        if not jobs_to_process:
            logger.info("leadgen_nothing_to_process")
            return AgentResult(data=[], count=0)

        jobs_to_process = jobs_to_process[:self.max_per_day]
        logger.info("leadgen_start", jobs=len(jobs_to_process), threshold=self.threshold)

        page = await new_page(self.browser_ctx)
        total_contacts = 0
        errors: list[str] = []

        for i, job in enumerate(jobs_to_process):
            company = job.get("company", "").strip()
            if not company:
                continue

            try:
                contacts = await self._find_contacts(page, company)

                saved = 0
                for contact in contacts[:self.contacts_per_job]:
                    contact["id"] = str(uuid.uuid4())[:8]
                    contact["job_id"] = job["id"]
                    self.db.insert_contact(contact)
                    saved += 1
                total_contacts += saved

                logger.info("leadgen_job_done",
                            progress=f"{i+1}/{len(jobs_to_process)}",
                            company=company[:30],
                            contacts_saved=saved)

            except Exception as e:
                errors.append(f"{company}: {str(e)}")
                logger.warning("leadgen_failed", company=company, error=str(e))

            delay = random.uniform(self.delay_min, self.delay_max)
            await asyncio.sleep(delay)

        await page.close()

        logger.info("leadgen_complete",
                     total_contacts=total_contacts,
                     jobs_processed=len(jobs_to_process),
                     errors=len(errors),
                     strategy_stats=self._strategy_stats)

        return AgentResult(
            data={
                "contacts_found": total_contacts,
                "jobs_processed": len(jobs_to_process),
                "strategy_stats": self._strategy_stats,
            },
            count=total_contacts,
            errors=errors,
        )

    async def _find_contacts(self, page: Page, company: str) -> list[dict]:
        """Try multiple strategies to find CURRENT employees at a company."""

        # Strategy 1: Company LinkedIn page → People tab (best: only current employees)
        contacts = await self._strategy_company_people_tab(page, company)
        if contacts:
            self._strategy_stats["company_page"] += 1
            return contacts

        # Strategy 2: LinkedIn People Search (with current-company verification)
        contacts = await self._strategy_people_search(page, company)
        if contacts:
            self._strategy_stats["people_search"] += 1
            return contacts

        # Strategy 3: Broad link scan
        contacts = await self._strategy_link_scan(page, company)
        if contacts:
            self._strategy_stats["text_scan"] += 1
            return contacts

        self._strategy_stats["failed"] += 1
        logger.warning("leadgen_all_strategies_failed", company=company)
        return []

    async def _strategy_company_people_tab(self, page: Page, company: str) -> list[dict]:
        """Strategy 1: Navigate to company's LinkedIn page → People tab.

        This is the most reliable because it ONLY shows current employees.
        """
        # Search for the company on LinkedIn
        search_url = (
            f"https://www.linkedin.com/search/results/companies/"
            f"?keywords={quote_plus(company)}"
        )
        if not await safe_goto(page, search_url, timeout=15000):
            return []

        await human_delay(2, 3)

        # Find and click the first company result
        try:
            company_link = await page.query_selector("a[href*='/company/']")
            if not company_link:
                return []

            href = await company_link.get_attribute("href")
            if not href:
                return []

            company_slug = href.split("/company/")[1].split("/")[0].split("?")[0]
            people_url = f"https://www.linkedin.com/company/{company_slug}/people/"

            if not await safe_goto(page, people_url, timeout=15000):
                return []

            await human_delay(2, 4)
            await random_scroll(page, scrolls=2)

            # Extract people from the company people page
            contacts = await self._extract_profile_links(page, company, source="company_page")

            if contacts:
                logger.info("leadgen_company_page_success", company=company, count=len(contacts))

            return contacts

        except Exception as e:
            logger.debug("leadgen_company_page_failed", company=company, error=str(e))
            return []

    async def _strategy_people_search(self, page: Page, company: str) -> list[dict]:
        """Strategy 2: LinkedIn People Search with boolean operators.

        No quotes around company name — they're too restrictive.
        Verify each contact currently works at the company.
        """
        contacts = []

        for search_template in SEARCH_TEMPLATES:
            if len(contacts) >= self.contacts_per_job:
                break

            # Boolean search: role keywords AND company (no quotes around company)
            query = f"{search_template} {company}"
            search_url = (
                f"https://www.linkedin.com/search/results/people/"
                f"?keywords={quote_plus(query)}&origin=GLOBAL_SEARCH_HEADER"
            )

            if not await safe_goto(page, search_url, timeout=15000):
                continue

            await human_delay(2, 4)
            await random_scroll(page, scrolls=2)

            # Extract and verify contacts
            found = await self._extract_profile_links(page, company, source="people_search")
            contacts.extend(found)

            await human_delay(2, 3)

        return contacts

    async def _strategy_link_scan(self, page: Page, company: str) -> list[dict]:
        """Strategy 3: Simple scan for /in/ links on the current page."""
        return await self._extract_profile_links(page, company, source="link_scan")

    async def _extract_profile_links(self, page: Page, company: str,
                                      source: str = "") -> list[dict]:
        """Extract LinkedIn profile links from the current page.

        Verifies each person currently works at the company by checking
        their headline/subtitle text for the company name.
        """
        contacts = []
        company_lower = company.lower()
        seen_urls = set()

        try:
            # Find all profile links
            links = await page.query_selector_all("a[href*='/in/']")

            for link in links[:30]:
                if len(contacts) >= self.contacts_per_job:
                    break

                try:
                    href = await link.get_attribute("href")
                    if not href or "/in/" not in href:
                        continue

                    # Clean URL
                    profile_url = href.split("?")[0]
                    if not profile_url.startswith("http"):
                        profile_url = "https://www.linkedin.com" + profile_url

                    # Dedup
                    if profile_url in seen_urls:
                        continue
                    seen_urls.add(profile_url)

                    # Get name
                    name = (await link.inner_text()).strip()
                    if not name or len(name) < 2 or name.lower() == "linkedin member":
                        continue

                    # Get context (title/headline) from parent element
                    title = ""
                    parent_text = ""
                    try:
                        parent = await link.evaluate_handle(
                            "el => el.closest('li') || el.closest('div[class*=\"result\"]') || el.parentElement?.parentElement"
                        )
                        if parent:
                            parent_text = await parent.evaluate("el => el.innerText || ''")
                    except Exception:
                        pass

                    # Extract title from parent text
                    if parent_text:
                        lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
                        for j, line in enumerate(lines):
                            if name.split()[0] in line and j + 1 < len(lines):
                                title = lines[j + 1][:120]
                                break

                    # VERIFY: Does this person CURRENTLY work at the company?
                    context = f"{title} {parent_text}".lower()
                    if not self._verify_current_employee(context, company_lower):
                        self._strategy_stats["skipped_former_employee"] += 1
                        continue

                    # Determine relevance
                    relevance, confidence = self._assess_relevance(title, name)

                    contacts.append({
                        "name": name[:60],
                        "title": title,
                        "linkedin_url": profile_url,
                        "relevance_reason": relevance,
                        "confidence": confidence,
                    })

                except Exception:
                    continue

        except Exception as e:
            logger.debug("leadgen_extract_failed", error=str(e))

        return contacts

    @staticmethod
    def _verify_current_employee(context: str, company_lower: str) -> bool:
        """Check if context text suggests the person CURRENTLY works at the company.

        Returns True if company name appears in the context (headline, subtitle).
        The Company People page only shows current employees, so this is more
        of a sanity check for the People Search strategy.
        """
        if not context:
            return False
        # Check if company name (or parts of it) appears in context
        # Handle multi-word company names: check if the main part matches
        company_parts = company_lower.split()
        main_name = company_parts[0] if company_parts else company_lower

        # Full name match
        if company_lower in context:
            return True
        # Main word match (e.g., "Flipkart" from "Flipkart Internet Pvt Ltd")
        if len(main_name) >= 4 and main_name in context:
            return True

        return False

    @staticmethod
    def _assess_relevance(title: str, name: str) -> tuple:
        """Assess how relevant a contact is based on their title.

        Returns: (relevance_reason, confidence)
        """
        title_lower = title.lower()

        if any(w in title_lower for w in ["recruiter", "recruiting", "talent acquisition"]):
            return "Recruiter — can refer you directly", "high"
        elif any(w in title_lower for w in ["hr ", "human resources", "people ops", "people operations"]):
            return "HR — handles hiring process", "high"
        elif any(w in title_lower for w in ["product manager", "product lead", "head of product", "director of product"]):
            return "Product team — potential hiring manager or peer", "high"
        elif any(w in title_lower for w in ["engineering manager", "tech lead", "director of engineering"]):
            return "Engineering leader — often part of PM hiring loops", "medium"
        elif any(w in title_lower for w in ["founder", "ceo", "cto", "coo", "vp"]):
            return "Leadership — decision maker", "medium"
        else:
            return f"Works at company", "low"

    @staticmethod
    def _is_duplicate(contact: dict, existing: list[dict]) -> bool:
        url = contact.get("linkedin_url", "")
        name = contact.get("name", "").lower()
        for e in existing:
            if e.get("linkedin_url") == url or e.get("name", "").lower() == name:
                return True
        return False
