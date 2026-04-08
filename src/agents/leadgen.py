"""Lead Gen Agent — finds relevant contacts at shortlisted companies.

For jobs scoring above the threshold, searches LinkedIn for:
- Recruiters / Talent Acquisition at the company
- Hiring managers (Engineering Managers, PM Leads)
- HR contacts

Rate-limited: max 20 companies/day, 5-8s delay between searches.
"""

import asyncio
import json
import random
import re
import uuid
from typing import Any, Optional

from playwright.async_api import BrowserContext, Page

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.browser import human_delay, new_page, safe_goto
from src.core.db import Database

logger = structlog.get_logger()


class LeadGenAgent(BaseAgent):
    """Find relevant contacts at shortlisted companies via LinkedIn."""

    name = "leadgen"

    def __init__(self, config: dict, db: Database, browser_ctx: BrowserContext):
        super().__init__(config)
        self.db = db
        self.browser_ctx = browser_ctx

        lead_config = config.get("lead_gen", {})
        self.contacts_per_job = lead_config.get("contacts_per_job", 3)
        self.search_roles = lead_config.get("search_roles", [
            "Recruiter", "Talent Acquisition", "Engineering Manager", "HR"
        ])
        delay_range = lead_config.get("delay_between_searches_s", [5, 8])
        self.delay_min = delay_range[0] if isinstance(delay_range, list) else 5
        self.delay_max = delay_range[1] if isinstance(delay_range, list) and len(delay_range) > 1 else 8

        match_config = config.get("matching", {})
        self.threshold = match_config.get("shortlist_threshold", 70)
        self.max_per_day = match_config.get("max_shortlist_per_day", 20)

    async def run(self, input_data: Any = None) -> AgentResult:
        """Find contacts for shortlisted jobs (score >= threshold)."""
        # Get shortlisted jobs that don't have contacts yet
        scored_jobs = self.db.get_jobs(parse_status="parsed", min_score=self.threshold)

        # Filter out jobs that already have contacts
        jobs_to_process = []
        for job in scored_jobs:
            existing = self.db.get_contacts_for_job(job["id"])
            if not existing:
                jobs_to_process.append(job)

        if not jobs_to_process:
            logger.info("leadgen_nothing_to_process")
            return AgentResult(data=[], count=0)

        # Cap at max per day
        jobs_to_process = jobs_to_process[:self.max_per_day]
        logger.info("leadgen_start", jobs=len(jobs_to_process), threshold=self.threshold)

        page = await new_page(self.browser_ctx)
        total_contacts = 0
        errors: list[str] = []

        for i, job in enumerate(jobs_to_process):
            company = job.get("company", "")
            if not company:
                continue

            try:
                contacts = await self._search_contacts(page, company, job.get("title", ""))

                for contact in contacts[:self.contacts_per_job]:
                    contact["id"] = str(uuid.uuid4())[:8]
                    contact["job_id"] = job["id"]
                    self.db.insert_contact(contact)
                    total_contacts += 1

                logger.info("leadgen_job_done",
                            progress=f"{i+1}/{len(jobs_to_process)}",
                            company=company[:30],
                            contacts=len(contacts))

            except Exception as e:
                errors.append(f"{company}: {str(e)}")
                logger.warning("leadgen_failed", company=company, error=str(e))

            # Rate limit
            delay = random.uniform(self.delay_min, self.delay_max)
            await asyncio.sleep(delay)

        await page.close()
        logger.info("leadgen_complete", contacts=total_contacts, errors=len(errors))

        return AgentResult(
            data={"contacts_found": total_contacts},
            count=total_contacts,
            errors=errors,
        )

    async def _search_contacts(self, page: Page, company: str, job_title: str) -> list[dict]:
        """Search LinkedIn People for contacts at a company."""
        contacts = []

        for role in self.search_roles:
            if len(contacts) >= self.contacts_per_job:
                break

            query = f"{company} {role}"
            search_url = f"https://www.linkedin.com/search/results/people/?keywords={query.replace(' ', '%20')}"

            if not await safe_goto(page, search_url, timeout=15000):
                continue

            await human_delay(2, 3)

            try:
                # Extract people cards from search results
                cards = await page.query_selector_all(
                    "div.entity-result, li.reusable-search__result-container, "
                    "div[data-view-name='search-entity-result-universal-template']"
                )

                for card in cards[:3]:  # Top 3 results per role search
                    try:
                        contact = await self._parse_person_card(card, role)
                        if contact and contact not in contacts:
                            contacts.append(contact)
                    except Exception:
                        continue

            except Exception as e:
                logger.debug("leadgen_search_failed", query=query, error=str(e))

            await human_delay(1, 2)

        return contacts

    async def _parse_person_card(self, card, search_role: str) -> Optional[dict]:
        """Parse a LinkedIn people search result card."""
        # Name
        name_el = await card.query_selector(
            "span.entity-result__title-text a span[aria-hidden='true'], "
            "span.entity-result__title-text a, "
            "a.app-aware-link span[dir='ltr']"
        )
        name = (await name_el.inner_text()).strip() if name_el else ""
        if not name or name.lower() == "linkedin member":
            return None

        # Profile URL
        link_el = await card.query_selector(
            "a.app-aware-link[href*='/in/'], "
            "a[href*='/in/']"
        )
        linkedin_url = ""
        if link_el:
            href = await link_el.get_attribute("href")
            if href:
                linkedin_url = href.split("?")[0]
                if not linkedin_url.startswith("http"):
                    linkedin_url = "https://www.linkedin.com" + linkedin_url

        # Title/headline
        title_el = await card.query_selector(
            "div.entity-result__primary-subtitle, "
            "p.entity-result__summary, "
            "div.linked-area div.t-14"
        )
        title = (await title_el.inner_text()).strip() if title_el else ""

        if not linkedin_url:
            return None

        return {
            "name": name,
            "title": title[:100],
            "linkedin_url": linkedin_url,
            "relevance_reason": f"Found via '{search_role}' search",
            "confidence": "high" if search_role.lower() in title.lower() else "medium",
        }
