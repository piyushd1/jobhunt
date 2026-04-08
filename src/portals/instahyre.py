"""Instahyre portal adapter.

Scrapes Instahyre's candidate opportunities page using a logged-in persistent session.
Instahyre is an invite-only platform — requires active login with recommendations visible.
"""

import re
from urllib.parse import quote_plus

from typing import Optional

from playwright.async_api import Page

import structlog

from src.core.browser import human_delay, random_scroll
from src.portals.base import PortalAdapter, RawJob

logger = structlog.get_logger()


class InstahyreAdapter(PortalAdapter):
    """Scrape Instahyre candidate opportunities."""

    name = "instahyre"
    base_url = "https://www.instahyre.com/candidate/opportunities/"

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        locations = self.get_locations()
        jobs: list[RawJob] = []

        for location in locations:
            for keyword in keywords:
                if len(jobs) >= self.max_results:
                    break

                url = self._build_search_url(keyword, location)
                logger.info("instahyre_searching", keyword=keyword, location=location)

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await human_delay(2, 4)
                    await random_scroll(page, scrolls=3)

                    page_jobs = await self._extract_jobs(page)
                    jobs.extend(page_jobs)
                    logger.info("instahyre_keyword_done", keyword=keyword, location=location, found=len(page_jobs))

                except Exception as e:
                    logger.warning("instahyre_search_failed", keyword=keyword, location=location, error=str(e))
                    continue

                await human_delay(3, 6)

        seen_urls = set()
        unique_jobs = []
        for job in jobs:
            if job.url not in seen_urls:
                seen_urls.add(job.url)
                unique_jobs.append(job)

        return unique_jobs[:self.max_results]

    async def _extract_jobs(self, page: Page) -> list[RawJob]:
        """Extract job cards from Instahyre opportunities page."""
        jobs = []

        # Try multiple selector strategies for job cards
        card_selectors = [
            "div.opportunity-card",
            "div[class*='opportunity']",
            "div.job-card",
            "div[class*='jobCard']",
            "div[class*='job-listing']",
            "div.card.opportunity",
            "div[class*='opp-card']",
            "li[class*='opportunity']",
        ]

        cards = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
                    logger.debug("instahyre_selector_hit", selector=sel, count=len(found))
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

        # Fallback: extract job links from the page
        if not jobs:
            logger.info("instahyre_using_link_fallback")
            try:
                links = await page.query_selector_all(
                    "a[href*='/candidate/opportunity/'], a[href*='/job/'], "
                    "a[href*='/opportunity/']"
                )
                seen = set()
                for link in links:
                    href = await link.get_attribute("href")
                    text = (await link.inner_text()).strip()
                    if href and text and len(text) > 3 and href not in seen:
                        seen.add(href)
                        full_url = href if href.startswith("http") else f"https://www.instahyre.com{href}"
                        jobs.append(RawJob(url=full_url, title=text, source="Instahyre"))
            except Exception as e:
                logger.warning("instahyre_link_fallback_failed", error=str(e))

        # Final fallback: regex URL extraction from raw HTML
        if not jobs:
            logger.info("instahyre_using_url_regex_fallback")
            content = await page.content()
            urls = self._extract_job_urls(content)
            for url in urls:
                jobs.append(RawJob(url=url, source="Instahyre"))

        return jobs

    async def _parse_card(self, card) -> Optional[RawJob]:
        """Parse a single Instahyre opportunity card."""
        # Title + URL
        title_el = None
        for sel in ["a.opportunity-title", "h3 a", "a[class*='title']",
                     "a[class*='job-title']", "div.job-title a",
                     "a[href*='/candidate/opportunity/']",
                     "a[href*='/opportunity/']"]:
            title_el = await card.query_selector(sel)
            if title_el:
                break

        title = ""
        url = ""
        if title_el:
            title = (await title_el.inner_text()).strip()
            href = await title_el.get_attribute("href")
            if href:
                url = href if href.startswith("http") else f"https://www.instahyre.com{href}"

        # If no link found, try getting title from text and URL from data attribute
        if not url:
            # Try getting job URL from data attribute on the card
            job_id = await card.get_attribute("data-opportunity-id")
            if not job_id:
                job_id = await card.get_attribute("data-id")
            if job_id:
                url = f"https://www.instahyre.com/candidate/opportunity/{job_id}/"

            # Try getting title from heading element
            heading_el = await card.query_selector("h3, h4, div[class*='title']")
            if heading_el and not title:
                title = (await heading_el.inner_text()).strip()

        if not url:
            return None

        # Company
        company_el = await card.query_selector(
            "div.company-name, span.company-name, a[class*='company'], "
            "div[class*='company'], span[class*='companyName']"
        )
        company = (await company_el.inner_text()).strip() if company_el else ""

        # Location
        loc_el = await card.query_selector(
            "span.location, div.location, span[class*='location'], "
            "div[class*='location'], span.loc"
        )
        location = (await loc_el.inner_text()).strip() if loc_el else ""

        # Experience
        exp_el = await card.query_selector(
            "span.experience, div.experience, span[class*='experience'], "
            "div[class*='experience']"
        )
        experience = (await exp_el.inner_text()).strip() if exp_el else ""

        return RawJob(
            url=url,
            title=title,
            company=company,
            location=location,
            experience_required=experience,
            source="Instahyre",
        )

    def _build_search_url(self, keyword: str, location: str) -> str:
        """Build Instahyre opportunities search URL."""
        encoded_keyword = quote_plus(keyword)
        encoded_location = quote_plus(location)
        return (
            f"{self.base_url}"
            f"?search={encoded_keyword}"
            f"&location={encoded_location}"
        )

    @staticmethod
    def _extract_job_urls(html: str) -> list[str]:
        """Fallback regex extraction for Instahyre job URLs."""
        pattern = r'https://www\.instahyre\.com/candidate/opportunity/[^\s"\'<>]+'
        urls = list(set(re.findall(pattern, html)))
        # Also try generic job path
        alt_pattern = r'https://www\.instahyre\.com/job/[^\s"\'<>]+'
        urls.extend(set(re.findall(alt_pattern, html)))
        return list(set(urls))[:25]
