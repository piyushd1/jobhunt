"""Indeed India (indeed.co.in) portal adapter.

Scrapes Indeed's job search results using a logged-in persistent session.
Uses CSS selectors with regex URL fallback.
"""

import re
from urllib.parse import quote_plus

from typing import Optional

from playwright.async_api import Page

import structlog

from src.core.browser import human_delay, random_scroll
from src.portals.base import PortalAdapter, RawJob

logger = structlog.get_logger()


class IndeedAdapter(PortalAdapter):
    """Scrape Indeed India job search results."""

    name = "indeed"
    base_url = "https://www.indeed.co.in"

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        locations = self.get_locations()
        jobs: list[RawJob] = []

        for location in locations:
            for keyword in keywords:
                if len(jobs) >= self.max_results:
                    break

                for page_num in range(self.pages_per_search):
                    if len(jobs) >= self.max_results:
                        break

                    url = self._build_search_url(keyword, location, page_num=page_num)
                    logger.info("indeed_searching", keyword=keyword, location=location,
                                page=page_num + 1, total_so_far=len(jobs))

                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await human_delay(2, 4)
                        await random_scroll(page, scrolls=2)

                        page_jobs = await self._extract_jobs(page)
                        if not page_jobs:
                            break  # No more results on this page

                        jobs.extend(page_jobs)
                        logger.info("indeed_page_done", keyword=keyword, location=location,
                                    page=page_num + 1, found=len(page_jobs))

                    except Exception as e:
                        logger.warning("indeed_search_failed", keyword=keyword,
                                        location=location, page=page_num + 1, error=str(e))
                        break  # Stop paginating on error

                    await human_delay(3, 6)

        seen_urls = set()
        unique_jobs = []
        for job in jobs:
            if job.url not in seen_urls:
                seen_urls.add(job.url)
                unique_jobs.append(job)

        return unique_jobs[:self.max_results]

    async def _extract_jobs(self, page: Page) -> list[RawJob]:
        """Extract job cards from Indeed search results."""
        jobs = []

        # Try multiple selector strategies for job cards
        card_selectors = [
            "div.job_seen_beacon",
            "div.cardOutline",
            "div.slider_container",
            "td.resultContent",
            "div[class*='jobCard']",
            "div[class*='job_seen']",
            "li.css-1ac2h1w",
            "div[data-jk]",
        ]

        cards = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
                    logger.debug("indeed_selector_hit", selector=sel, count=len(found))
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
            logger.info("indeed_using_link_fallback")
            try:
                links = await page.query_selector_all(
                    "a[href*='/viewjob'], a[href*='jk='], a[id^='job_']"
                )
                seen = set()
                for link in links:
                    href = await link.get_attribute("href")
                    text = (await link.inner_text()).strip()
                    if href and text and len(text) > 3 and href not in seen:
                        seen.add(href)
                        full_url = href if href.startswith("http") else f"https://www.indeed.co.in{href}"
                        jobs.append(RawJob(url=full_url, title=text, source="Indeed"))
            except Exception as e:
                logger.warning("indeed_link_fallback_failed", error=str(e))

        # Final fallback: regex URL extraction from raw HTML
        if not jobs:
            logger.info("indeed_using_url_regex_fallback")
            content = await page.content()
            urls = self._extract_job_urls(content)
            for url in urls:
                jobs.append(RawJob(url=url, source="Indeed"))

        return jobs

    async def _parse_card(self, card) -> Optional[RawJob]:
        """Parse a single Indeed job card."""
        # Title + URL
        title_el = None
        for sel in ["h2.jobTitle a", "a[data-jk]", "h2 a", "a.jcs-JobTitle",
                     "a[class*='jobTitle']", "a[id^='job_']",
                     "a[href*='/viewjob']"]:
            title_el = await card.query_selector(sel)
            if title_el:
                break

        title = ""
        url = ""
        if title_el:
            # Indeed wraps title in a span inside the anchor
            title_span = await title_el.query_selector("span[title]")
            if title_span:
                title = (await title_span.get_attribute("title")) or ""
            if not title:
                title = (await title_el.inner_text()).strip()

            href = await title_el.get_attribute("href")
            if href:
                url = href if href.startswith("http") else f"https://www.indeed.co.in{href}"
                # Clean tracking params but keep the job key
                url = url.split("&from=")[0]

        if not url:
            return None

        # Company
        company_el = await card.query_selector(
            "span[data-testid='company-name'], span.companyName, "
            "span.company, a[data-tn-element='companyName'], "
            "div.company_location span.companyName"
        )
        company = (await company_el.inner_text()).strip() if company_el else ""

        # Location
        loc_el = await card.query_selector(
            "div[data-testid='text-location'], div.companyLocation, "
            "span.companyLocation, div[class*='companyLocation']"
        )
        location = (await loc_el.inner_text()).strip() if loc_el else ""

        # Snippet / description preview
        snippet_el = await card.query_selector(
            "div.job-snippet, div[class*='job-snippet'], "
            "table.jobCardShelfContainer, ul[style*='list-style']"
        )
        snippet = (await snippet_el.inner_text()).strip()[:200] if snippet_el else ""

        return RawJob(
            url=url,
            title=title,
            company=company,
            location=location,
            snippet=snippet,
            source="Indeed",
        )

    def _build_search_url(self, keyword: str, location: str, page_num: int = 0) -> str:
        """Build Indeed India job search URL with date filter and pagination.

        Date filter: fromage={days}
        Pagination: start=0, 10, 20, ... (10 results per page)
        """
        encoded_keyword = quote_plus(keyword)
        encoded_location = quote_plus(location)
        start = page_num * 10  # Indeed shows 10 results per page
        return (
            f"{self.base_url}/jobs"
            f"?q={encoded_keyword}"
            f"&l={encoded_location}"
            f"&sort=date"  # Sort by newest first
            f"&fromage={self.max_age_days}"
            f"&start={start}"
        )

    @staticmethod
    def _extract_job_urls(html: str) -> list[str]:
        """Fallback regex extraction for Indeed job URLs."""
        pattern = r'https://www\.indeed\.co\.in/viewjob[^\s"\'<>]+'
        urls = list(set(re.findall(pattern, html)))
        # Also try the /rc/clk pattern Indeed uses
        alt_pattern = r'https://www\.indeed\.co\.in/rc/clk[^\s"\'<>]+'
        urls.extend(set(re.findall(alt_pattern, html)))
        return list(set(urls))[:25]
