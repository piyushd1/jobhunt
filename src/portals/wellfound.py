"""Wellfound (formerly AngelList Talent) portal adapter — startup jobs."""

import re
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import Page

import structlog

from src.core.browser import human_delay, random_scroll
from src.portals.base import PortalAdapter, RawJob

logger = structlog.get_logger()


class WellfoundAdapter(PortalAdapter):
    """Scrape Wellfound job search results."""

    name = "wellfound"
    base_url = "https://wellfound.com/jobs"

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        locations = self.get_locations()
        jobs: list[RawJob] = []

        for location in locations:
            for keyword in keywords:
                if len(jobs) >= self.max_results:
                    break

                url = self._build_search_url(keyword, location)
                logger.info("wellfound_searching", keyword=keyword, location=location)

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await human_delay(2, 4)

                    # Wellfound uses infinite scroll — scroll multiple times to load more
                    for page_num in range(self.pages_per_search):
                        if len(jobs) >= self.max_results:
                            break

                        logger.info("wellfound_scroll_page", keyword=keyword,
                                    location=location, page=page_num + 1,
                                    total_so_far=len(jobs))

                        await random_scroll(page, scrolls=3)
                        await human_delay(2, 3)

                    page_jobs = await self._extract_jobs(page)
                    jobs.extend(page_jobs)
                    logger.info("wellfound_keyword_done", keyword=keyword, location=location, found=len(page_jobs))
                except Exception as e:
                    logger.warning("wellfound_search_failed", keyword=keyword, error=str(e))
                    continue

                await human_delay(3, 5)

        seen = set()
        return [j for j in jobs if j.url not in seen and not seen.add(j.url)][:self.max_results]

    async def _extract_jobs(self, page: Page) -> list[RawJob]:
        jobs = []

        card_selectors = [
            "div[class*='jobListingCard']", "div[class*='JobCard']",
            "a[class*='jobCard']", "div[data-test='job-card']",
            "div[class*='styles_component']",
        ]

        cards = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
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

        # Link-based fallback
        if not jobs:
            try:
                links = await page.query_selector_all("a[href*='/jobs/']")
                seen = set()
                for link in links:
                    href = await link.get_attribute("href")
                    text = (await link.inner_text()).strip()
                    if href and text and len(text) > 3 and href not in seen:
                        seen.add(href)
                        full_url = href if href.startswith("http") else f"https://wellfound.com{href}"
                        jobs.append(RawJob(url=full_url, title=text, source="Wellfound"))
            except Exception:
                pass

        # Regex fallback
        if not jobs:
            content = await page.content()
            urls = self._extract_job_urls(content)
            for u in urls:
                jobs.append(RawJob(url=u, source="Wellfound"))

        return jobs

    async def _parse_card(self, card) -> Optional[RawJob]:
        title_el = await card.query_selector("a[href*='/jobs/'] h2, a[href*='/jobs/'], h2, h3")
        if not title_el:
            return None

        title = (await title_el.inner_text()).strip()
        link_el = await card.query_selector("a[href*='/jobs/'], a[href*='/company/']")
        href = await link_el.get_attribute("href") if link_el else ""
        if not href:
            return None
        url = href if href.startswith("http") else f"https://wellfound.com{href}"

        company_el = await card.query_selector("a[href*='/company/'] span, span[class*='company'], h3")
        company = (await company_el.inner_text()).strip() if company_el else ""

        loc_el = await card.query_selector("span[class*='location'], div[class*='location']")
        location = (await loc_el.inner_text()).strip() if loc_el else ""

        return RawJob(url=url, title=title, company=company, location=location, source="Wellfound")

    def _build_search_url(self, keyword: str, location: str) -> str:
        """Build Wellfound job search URL with date filter.

        Date filter: datePosted=month (Wellfound only supports broad date filters)
        Pagination: Wellfound uses infinite scroll, handled via scroll loops in scrape().
        """
        return (
            f"{self.base_url}?role={quote_plus(keyword)}"
            f"&location={quote_plus(location)}"
            f"&datePosted=month"
        )

    @staticmethod
    def _extract_job_urls(html: str) -> list[str]:
        pattern = r'https://wellfound\.com/company/[^\s"\'<>]+/jobs/[^\s"\'<>]+'
        return list(set(re.findall(pattern, html)))[:25]
