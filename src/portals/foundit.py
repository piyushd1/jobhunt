"""Foundit.in (formerly Monster India) portal adapter.

Scrapes Foundit's job search results using a logged-in persistent session.
"""

import re
from urllib.parse import quote_plus

from typing import Optional

from playwright.async_api import Page

import structlog

from src.core.browser import human_delay, random_scroll
from src.portals.base import PortalAdapter, RawJob

logger = structlog.get_logger()


class FounditAdapter(PortalAdapter):
    """Scrape Foundit.in job search results."""

    name = "foundit"
    base_url = "https://www.foundit.in"

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        location = self.search_config.get("location", "India")
        jobs: list[RawJob] = []

        for keyword in keywords:
            if len(jobs) >= self.max_results:
                break

            url = self._build_search_url(keyword, location)
            logger.info("foundit_searching", keyword=keyword, location=location)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await human_delay(2, 4)
                await random_scroll(page, scrolls=2)

                page_jobs = await self._extract_jobs(page)
                jobs.extend(page_jobs)
                logger.info("foundit_keyword_done", keyword=keyword, found=len(page_jobs))

            except Exception as e:
                logger.warning("foundit_search_failed", keyword=keyword, error=str(e))
                continue

            await human_delay(3, 5)

        seen_urls = set()
        unique_jobs = []
        for job in jobs:
            if job.url not in seen_urls:
                seen_urls.add(job.url)
                unique_jobs.append(job)

        return unique_jobs[:self.max_results]

    async def _extract_jobs(self, page: Page) -> list[RawJob]:
        """Extract job cards from Foundit search results."""
        jobs = []

        # Try multiple selector strategies
        card_selectors = [
            "div.card-apply-content",
            "div.srpResultCardContainer",
            "div[class*='jobTuple']",
            "div.job-card",
            "div[class*='JobCard']",
            "div[class*='job-card']",
            "div[data-job-id]",
            "article[class*='job']",
        ]

        cards = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
                    logger.debug("foundit_selector_hit", selector=sel, count=len(found))
                    break
            except Exception:
                continue

        for card in cards:
            try:
                job = await self._parse_card(card)
                if job:
                    jobs.append(job)
            except Exception:
                continue

        # Fallback: extract ALL job-looking links from the page
        if not jobs:
            logger.info("foundit_using_link_fallback")
            try:
                links = await page.query_selector_all("a[href*='/job/'], a[href*='/jobs/']")
                seen = set()
                for link in links:
                    href = await link.get_attribute("href")
                    text = (await link.inner_text()).strip()
                    if href and text and len(text) > 3 and href not in seen:
                        seen.add(href)
                        full_url = href if href.startswith("http") else f"https://www.foundit.in{href}"
                        jobs.append(RawJob(url=full_url, title=text, source="Foundit"))
            except Exception as e:
                logger.warning("foundit_link_fallback_failed", error=str(e))

        # Final fallback: regex URL extraction from raw HTML
        if not jobs:
            logger.info("foundit_using_url_regex_fallback")
            content = await page.content()
            urls = self._extract_job_urls(content)
            for url in urls:
                jobs.append(RawJob(url=url, source="Foundit"))

        return jobs

    async def _parse_card(self, card) -> Optional[RawJob]:
        """Parse a single Foundit job card."""
        # Title + URL — try many selectors
        title_el = None
        for sel in ["a.card-job-detail", "a[class*='jobTitle']", "a.JobTitle",
                     "h3 a", "a[class*='job-title']", "a[class*='JobTitle']",
                     "a[href*='/job/']"]:
            title_el = await card.query_selector(sel)
            if title_el:
                break

        title = ""
        url = ""
        if title_el:
            title = (await title_el.inner_text()).strip()
            href = await title_el.get_attribute("href")
            if href:
                url = href if href.startswith("http") else f"https://www.foundit.in{href}"

        if not url:
            return None

        # Company
        company_el = await card.query_selector(
            "span.company-name, a[class*='companyName'], "
            "div.companyInfo a, span[class*='company']"
        )
        company = (await company_el.inner_text()).strip() if company_el else ""

        # Location
        loc_el = await card.query_selector(
            "span.loc, span[class*='location'], "
            "div.locWdth, span.location-text"
        )
        location = (await loc_el.inner_text()).strip() if loc_el else ""

        # Experience
        exp_el = await card.query_selector(
            "span.exp, span[class*='experience'], "
            "div.expwdth"
        )
        experience = (await exp_el.inner_text()).strip() if exp_el else ""

        return RawJob(
            url=url,
            title=title,
            company=company,
            location=location,
            experience_required=experience,
            source="Foundit",
        )

    def _build_search_url(self, keyword: str, location: str) -> str:
        """Build Foundit job search URL."""
        encoded_keyword = quote_plus(keyword)
        encoded_location = quote_plus(location)
        return (
            f"{self.base_url}/srp/results"
            f"?searchId=&query={encoded_keyword}"
            f"&locations={encoded_location}"
            f"&sort=1"  # Sort by relevance
        )

    @staticmethod
    def _extract_job_urls(html: str) -> list[str]:
        """Fallback regex extraction for Foundit job URLs."""
        pattern = r'https://www\.foundit\.in/job/[^\s"\'<>]+'
        urls = list(set(re.findall(pattern, html)))
        return urls[:25]
