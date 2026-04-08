"""Lead Gen Agent — finds relevant contacts at shortlisted companies.

Uses 3 strategies in order:
1. LinkedIn Company Page → People tab (most reliable)
2. LinkedIn People Search with broad selectors
3. Text/link scanning fallback (regex extraction from page HTML)

Rate-limited: max 20 companies/day, 5-8s delay between searches.
"""

import asyncio
import json
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
            "Recruiter", "Talent Acquisition", "HR", "Product Manager"
        ])
        delay_range = lead_config.get("delay_between_searches_s", [5, 8])
        self.delay_min = delay_range[0] if isinstance(delay_range, list) else 5
        self.delay_max = delay_range[1] if isinstance(delay_range, list) and len(delay_range) > 1 else 8

        match_config = config.get("matching", {})
        self.threshold = match_config.get("shortlist_threshold", 70)
        self.max_per_day = match_config.get("max_shortlist_per_day", 20)

        # Eval metrics
        self._strategy_stats = {"company_page": 0, "people_search": 0, "text_scan": 0, "failed": 0}

    async def run(self, input_data: Any = None) -> AgentResult:
        """Find contacts for shortlisted jobs (score >= threshold)."""
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
                contacts = await self._find_contacts_multi_strategy(
                    page, company, job.get("title", "")
                )

                for contact in contacts[:self.contacts_per_job]:
                    contact["id"] = str(uuid.uuid4())[:8]
                    contact["job_id"] = job["id"]
                    self.db.insert_contact(contact)
                    total_contacts += 1

                logger.info("leadgen_job_done",
                            progress=f"{i+1}/{len(jobs_to_process)}",
                            company=company[:30],
                            contacts_found=len(contacts))

            except Exception as e:
                errors.append(f"{company}: {str(e)}")
                logger.warning("leadgen_failed", company=company, error=str(e))

            delay = random.uniform(self.delay_min, self.delay_max)
            await asyncio.sleep(delay)

        await page.close()

        logger.info("leadgen_complete",
                     contacts=total_contacts, errors=len(errors),
                     strategy_stats=self._strategy_stats)

        return AgentResult(
            data={
                "contacts_found": total_contacts,
                "strategy_stats": self._strategy_stats,
            },
            count=total_contacts,
            errors=errors,
        )

    async def _find_contacts_multi_strategy(self, page: Page, company: str,
                                             job_title: str) -> list[dict]:
        """Try multiple strategies to find contacts at a company."""
        contacts = []

        # Strategy 1: LinkedIn People Search (most direct)
        contacts = await self._strategy_people_search(page, company)
        if contacts:
            self._strategy_stats["people_search"] += 1
            return contacts

        # Strategy 2: Text/link scan of search results page
        contacts = await self._strategy_text_scan(page, company)
        if contacts:
            self._strategy_stats["text_scan"] += 1
            return contacts

        # Strategy 3: LinkedIn Company Page → People tab
        contacts = await self._strategy_company_page(page, company)
        if contacts:
            self._strategy_stats["company_page"] += 1
            return contacts

        self._strategy_stats["failed"] += 1
        logger.warning("leadgen_all_strategies_failed", company=company)
        return []

    async def _strategy_people_search(self, page: Page, company: str) -> list[dict]:
        """Strategy 1: LinkedIn People Search with multiple selector fallbacks."""
        contacts = []

        for role in self.search_roles:
            if len(contacts) >= self.contacts_per_job:
                break

            query = f'"{company}" {role}'
            search_url = (
                f"https://www.linkedin.com/search/results/people/"
                f"?keywords={quote_plus(query)}&origin=GLOBAL_SEARCH_HEADER"
            )

            if not await safe_goto(page, search_url, timeout=20000):
                continue

            await human_delay(2, 4)
            await random_scroll(page, scrolls=2)

            # Try multiple selector strategies for people cards
            card_selectors = [
                "li.reusable-search__result-container",
                "div.entity-result",
                "div[data-view-name*='search-entity']",
                "li[class*='search-result']",
                "div.search-result__wrapper",
                "ul.reusable-search__entity-result-list > li",
            ]

            cards = []
            for sel in card_selectors:
                try:
                    found = await page.query_selector_all(sel)
                    if found and len(found) > 0:
                        cards = found
                        logger.debug("leadgen_selector_hit", selector=sel, count=len(found))
                        break
                except Exception:
                    continue

            for card in cards[:3]:
                try:
                    contact = await self._parse_person_card(card, role)
                    if contact and not self._is_duplicate(contact, contacts):
                        contacts.append(contact)
                except Exception:
                    continue

            await human_delay(2, 3)

        return contacts

    async def _strategy_text_scan(self, page: Page, company: str) -> list[dict]:
        """Strategy 2: Scan the current page for LinkedIn profile links + names."""
        contacts = []

        try:
            # Extract all links to LinkedIn profiles
            links = await page.query_selector_all("a[href*='/in/']")

            for link in links[:20]:
                try:
                    href = await link.get_attribute("href")
                    if not href or "/in/" not in href:
                        continue

                    # Clean the URL
                    profile_url = href.split("?")[0]
                    if not profile_url.startswith("http"):
                        profile_url = "https://www.linkedin.com" + profile_url

                    # Skip if it's not a real profile URL
                    if "/in/ACo" in profile_url or len(profile_url) < 30:
                        continue

                    # Try to get the name from the link text
                    text = (await link.inner_text()).strip()
                    if not text or len(text) < 2 or text.lower() == "linkedin member":
                        continue

                    # Get parent context for title
                    parent = await link.evaluate_handle("el => el.closest('li') || el.parentElement")
                    parent_text = ""
                    if parent:
                        try:
                            parent_text = await parent.evaluate("el => el.innerText")
                        except Exception:
                            pass

                    # Extract title from parent text (line after the name usually)
                    title = ""
                    if parent_text:
                        lines = [l.strip() for l in parent_text.split("\n") if l.strip()]
                        for j, line in enumerate(lines):
                            if text in line and j + 1 < len(lines):
                                title = lines[j + 1][:100]
                                break

                    contacts.append({
                        "name": text[:60],
                        "title": title,
                        "linkedin_url": profile_url,
                        "relevance_reason": f"Found at {company} via profile scan",
                        "confidence": "medium",
                    })

                    if len(contacts) >= self.contacts_per_job:
                        break

                except Exception:
                    continue

        except Exception as e:
            logger.debug("leadgen_text_scan_failed", error=str(e))

        return contacts

    async def _strategy_company_page(self, page: Page, company: str) -> list[dict]:
        """Strategy 3: Visit the company's LinkedIn page → People tab."""
        contacts = []

        # First search for the company page
        company_search_url = (
            f"https://www.linkedin.com/search/results/companies/"
            f"?keywords={quote_plus(company)}"
        )

        if not await safe_goto(page, company_search_url, timeout=15000):
            return []

        await human_delay(2, 3)

        # Click on the first company result
        try:
            company_link = await page.query_selector(
                "a[href*='/company/'], "
                "a.app-aware-link[href*='/company/']"
            )
            if company_link:
                href = await company_link.get_attribute("href")
                if href:
                    company_url = href.split("?")[0]
                    if not company_url.startswith("http"):
                        company_url = "https://www.linkedin.com" + company_url

                    # Go to company's People tab
                    people_url = f"{company_url}/people/"
                    if not await safe_goto(page, people_url, timeout=15000):
                        return []

                    await human_delay(2, 3)
                    await random_scroll(page, scrolls=2)

                    # Extract people from the company page
                    contacts = await self._strategy_text_scan(page, company)
        except Exception as e:
            logger.debug("leadgen_company_page_failed", error=str(e))

        return contacts

    async def _parse_person_card(self, card, search_role: str) -> Optional[dict]:
        """Parse a LinkedIn people search result card with multiple selector fallbacks."""
        # Name — try many selectors
        name = ""
        name_selectors = [
            "span.entity-result__title-text a span[aria-hidden='true']",
            "span.entity-result__title-text a",
            "a.app-aware-link span[dir='ltr']",
            "span[dir='ltr']",
            "a[href*='/in/'] span",
            "a[href*='/in/']",
        ]
        for sel in name_selectors:
            try:
                el = await card.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) > 1 and text.lower() != "linkedin member":
                        name = text
                        break
            except Exception:
                continue

        if not name:
            return None

        # Profile URL
        linkedin_url = ""
        try:
            link_el = await card.query_selector("a[href*='/in/']")
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    linkedin_url = href.split("?")[0]
                    if not linkedin_url.startswith("http"):
                        linkedin_url = "https://www.linkedin.com" + linkedin_url
        except Exception:
            pass

        if not linkedin_url:
            return None

        # Title/headline
        title = ""
        title_selectors = [
            "div.entity-result__primary-subtitle",
            "p.entity-result__summary",
            "div.linked-area div.t-14",
            "div.entity-result__content div.t-14",
        ]
        for sel in title_selectors:
            try:
                el = await card.query_selector(sel)
                if el:
                    title = (await el.inner_text()).strip()[:100]
                    break
            except Exception:
                continue

        return {
            "name": name[:60],
            "title": title,
            "linkedin_url": linkedin_url,
            "relevance_reason": f"Found via '{search_role}' search",
            "confidence": "high" if search_role.lower() in title.lower() else "medium",
        }

    @staticmethod
    def _is_duplicate(contact: dict, existing: list[dict]) -> bool:
        """Check if a contact is already in the list."""
        url = contact.get("linkedin_url", "")
        name = contact.get("name", "").lower()
        for e in existing:
            if e.get("linkedin_url") == url or e.get("name", "").lower() == name:
                return True
        return False
