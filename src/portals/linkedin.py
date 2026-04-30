"""LinkedIn Jobs portal adapter.

Scrapes LinkedIn's job search results page using a logged-in persistent session.
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


class LinkedInAdapter(PortalAdapter):
    """Scrape LinkedIn Jobs search results."""

    name = "linkedin"
    base_url = "https://www.linkedin.com/jobs/search/"

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        locations = self.get_locations()
        jobs: list[RawJob] = []

        for location in locations:
            for keyword in keywords:
                if len(jobs) >= self.max_results:
                    break

                # Paginate through multiple pages per keyword+location
                seen_in_keyword: set[str] = set()
                for page_num in range(self.pages_per_search):
                    if len(jobs) >= self.max_results:
                        break

                    url = self._build_search_url(keyword, location, page_num=page_num)
                    logger.info("linkedin_searching", keyword=keyword, location=location,
                                page=page_num + 1, total_so_far=len(jobs))

                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await human_delay(2, 4)
                        await random_scroll(page, scrolls=3)

                        page_jobs = await self._extract_jobs(page, keyword)
                        if not page_jobs:
                            break  # No more results on this page

                        # Early-stop: if all results are duplicates of prior pages,
                        # the portal likely redirected back to page 1.
                        new_jobs = [j for j in page_jobs if j.url not in seen_in_keyword]
                        if page_num > 0 and not new_jobs:
                            logger.info("linkedin_no_new_jobs_stop", keyword=keyword,
                                        location=location, page=page_num + 1)
                            break

                        for j in new_jobs:
                            seen_in_keyword.add(j.url)
                        jobs.extend(new_jobs)
                        logger.info("linkedin_page_done", keyword=keyword, location=location,
                                    page=page_num + 1, found=len(new_jobs))

                    except Exception as e:
                        logger.warning("linkedin_search_failed", keyword=keyword,
                                        location=location, page=page_num + 1, error=str(e))
                        break  # Stop paginating on error

                    await human_delay(3, 6)

        # Deduplicate by URL
        seen_urls = set()
        unique_jobs = []
        for job in jobs:
            if job.url not in seen_urls:
                seen_urls.add(job.url)
                unique_jobs.append(job)

        return unique_jobs[:self.max_results]

    async def _extract_jobs(self, page: Page, keyword: str) -> list[RawJob]:
        """Extract job cards from LinkedIn search results."""
        jobs = []

        # Primary: CSS selector extraction
        try:
            # LinkedIn job cards in search results
            cards = await page.query_selector_all(
                "div.job-card-container, li.jobs-search-results__list-item, "
                "div.jobs-search-results-list__list-item"
            )

            for card in cards:
                try:
                    job = await self._parse_card(card)
                    if job:
                        jobs.append(job)
                except Exception:
                    continue

        except Exception as e:
            logger.warning("linkedin_selector_failed", error=str(e))

        # Fallback: regex URL extraction if selectors returned nothing
        if not jobs:
            logger.info("linkedin_using_url_fallback")
            content = await page.content()
            urls = self._extract_job_urls(content)
            for url in urls:
                jobs.append(RawJob(url=url, source="LinkedIn"))

        return jobs

    async def _parse_card(self, card) -> Optional[RawJob]:
        """Parse a single LinkedIn job card element."""
        # Title
        title_el = await card.query_selector(
            "a.job-card-list__title, a.job-card-container__link, "
            "strong, a[data-control-name='job_card']"
        )
        title = (await title_el.inner_text()).strip() if title_el else ""

        # URL
        link_el = await card.query_selector(
            "a.job-card-list__title, a.job-card-container__link, "
            "a[href*='/jobs/view/']"
        )
        url = ""
        if link_el:
            href = await link_el.get_attribute("href")
            if href:
                # Normalize LinkedIn job URL
                url = href.split("?")[0]
                if not url.startswith("http"):
                    url = "https://www.linkedin.com" + url

        if not url:
            return None

        # Company
        company_el = await card.query_selector(
            "span.job-card-container__primary-description, "
            "a.job-card-container__company-name, "
            "div.artdeco-entity-lockup__subtitle"
        )
        company = (await company_el.inner_text()).strip() if company_el else ""

        # Location
        loc_el = await card.query_selector(
            "li.job-card-container__metadata-item, "
            "span.job-card-container__metadata-wrapper, "
            "div.artdeco-entity-lockup__caption"
        )
        location = (await loc_el.inner_text()).strip() if loc_el else ""

        return RawJob(
            url=url,
            title=title,
            company=company,
            location=location,
            source="LinkedIn",
        )

    def _experience_filter(self) -> str:
        """Map our experience_min/max to LinkedIn's f_E levels.

        LinkedIn experience levels (f_E):
          1 = Internship   (~0 yrs)
          2 = Entry level  (0-2 yrs)
          3 = Associate    (2-5 yrs)
          4 = Mid-Senior   (5-10 yrs)
          5 = Director     (10-15 yrs)
          6 = Executive    (15+ yrs)

        Returns "&f_E=2&f_E=3" style. Empty string if not configured.
        """
        emin = self.search_config.get("experience_min")
        emax = self.search_config.get("experience_max")
        if emin is None or emax is None:
            return ""

        # LinkedIn level → year range it represents (overlap-friendly)
        level_ranges = {
            1: (0, 1),
            2: (0, 2),
            3: (2, 5),
            4: (5, 10),
            5: (10, 15),
            6: (15, 99),
        }
        chosen = []
        for level, (lo, hi) in level_ranges.items():
            # Overlap test between [emin, emax] and [lo, hi]
            if lo <= emax and hi >= emin:
                chosen.append(level)
        return "".join(f"&f_E={lvl}" for lvl in chosen)

    def _build_search_url(self, keyword: str, location: str, page_num: int = 0) -> str:
        """Build LinkedIn jobs search URL with date filter and pagination.

        LinkedIn date filters (f_TPR):
          r86400   = past 24 hours
          r604800  = past week
          r1296000 = past 15 days (custom)
          r2592000 = past month
        Pagination: start=0, 25, 50, ...
        """
        encoded_keyword = quote_plus(keyword)
        # When location == "Remote", use LinkedIn's remote workplace filter
        # f_WT=2 (Remote) instead of a geographic location.
        is_remote = location.strip().lower() == "remote"
        # Convert max_age_days to seconds for LinkedIn's f_TPR parameter
        age_seconds = self.max_age_days * 86400
        start = page_num * 25  # LinkedIn shows 25 results per page
        exp_filter = self._experience_filter()
        if is_remote:
            return (
                f"{self.base_url}?keywords={encoded_keyword}"
                f"&location=India"          # broad geo so LinkedIn returns results
                f"&f_WT=2"                  # workplace type: Remote
                f"{exp_filter}"             # experience level filter
                f"&sortBy=DD"
                f"&f_TPR=r{age_seconds}"
                f"&start={start}"
            )
        encoded_location = quote_plus(location)
        return (
            f"{self.base_url}?keywords={encoded_keyword}"
            f"&location={encoded_location}"
            f"{exp_filter}"
            f"&sortBy=DD"
            f"&f_TPR=r{age_seconds}"
            f"&start={start}"
        )

    @staticmethod
    def _extract_job_urls(html: str) -> list[str]:
        """Fallback regex extraction for LinkedIn job URLs."""
        pattern = r'https://www\.linkedin\.com/jobs/view/\d+'
        urls = list(set(re.findall(pattern, html)))
        return urls[:25]
