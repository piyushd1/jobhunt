"""Hirist.tech portal adapter.

Scrapes Hirist's job search results using a logged-in persistent session.
Hirist is a tech-focused job portal (formerly iimjobs tech division).
"""

import re
from urllib.parse import quote_plus

from typing import Optional

from playwright.async_api import Page

import structlog

from src.core.browser import human_delay, random_scroll
from src.portals.base import PortalAdapter, RawJob

logger = structlog.get_logger()


class HiristAdapter(PortalAdapter):
    """Scrape Hirist.tech job search results."""

    name = "hirist"
    base_url = "https://www.hirist.tech"

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        locations = self.get_locations()
        experience = self.search_config.get(
            "experience_min",
            self.search_config.get("experience_years", 5),
        )
        jobs: list[RawJob] = []

        for location in locations:
            for keyword in keywords:
                if len(jobs) >= self.max_results:
                    break

                seen_in_keyword: set[str] = set()
                for page_num in range(self.pages_per_search):
                    if len(jobs) >= self.max_results:
                        break

                    url = self._build_search_url(keyword, location, experience, page_num=page_num)
                    logger.info("hirist_searching", keyword=keyword, location=location,
                                page=page_num + 1, total_so_far=len(jobs))

                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await human_delay(2, 4)
                        await random_scroll(page, scrolls=2)

                        page_jobs = await self._extract_jobs(page)
                        if not page_jobs:
                            break

                        new_jobs = [j for j in page_jobs if j.url not in seen_in_keyword]
                        if page_num > 0 and not new_jobs:
                            logger.info("hirist_no_new_jobs_stop", keyword=keyword,
                                        location=location, page=page_num + 1)
                            break

                        for j in new_jobs:
                            seen_in_keyword.add(j.url)
                        jobs.extend(new_jobs)
                        logger.info("hirist_page_done", keyword=keyword, location=location,
                                    page=page_num + 1, found=len(new_jobs))

                    except Exception as e:
                        logger.warning("hirist_search_failed", keyword=keyword,
                                        location=location, page=page_num + 1, error=str(e))
                        break

                    await human_delay(3, 5)

        seen_urls = set()
        unique_jobs = []
        for job in jobs:
            if job.url not in seen_urls:
                seen_urls.add(job.url)
                unique_jobs.append(job)

        return unique_jobs[:self.max_results]

    async def _extract_jobs(self, page: Page) -> list[RawJob]:
        """Extract job cards from Hirist search results."""
        jobs = []

        # Try multiple selector strategies for job cards
        card_selectors = [
            "div.job-card",
            "div[class*='jobCard']",
            "div[class*='job-tuple']",
            "div.search-result-card",
            "div[class*='srp-card']",
            "div[class*='listing-card']",
            "li[class*='job-card']",
            "div[data-job-id]",
        ]

        cards = []
        for sel in card_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    cards = found
                    logger.debug("hirist_selector_hit", selector=sel, count=len(found))
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
            logger.info("hirist_using_link_fallback")
            try:
                links = await page.query_selector_all(
                    "a[href*='/job/'], a[href*='/jobs/'], "
                    "a[href*='-jobs-'], a[href*='job-detail']"
                )
                seen = set()
                for link in links:
                    href = await link.get_attribute("href")
                    text = (await link.inner_text()).strip()
                    if href and text and len(text) > 3 and href not in seen:
                        seen.add(href)
                        full_url = href if href.startswith("http") else f"https://www.hirist.tech{href}"
                        jobs.append(RawJob(url=full_url, title=text, source="Hirist"))
            except Exception as e:
                logger.warning("hirist_link_fallback_failed", error=str(e))

        # Final fallback: regex URL extraction from raw HTML
        if not jobs:
            logger.info("hirist_using_url_regex_fallback")
            content = await page.content()
            urls = self._extract_job_urls(content)
            for url in urls:
                jobs.append(RawJob(url=url, source="Hirist"))

        return jobs

    async def _parse_card(self, card) -> Optional[RawJob]:
        """Parse a single Hirist job card."""
        # Title + URL
        title_el = None
        for sel in ["a.job-title", "h3 a", "a[class*='title']",
                     "a[class*='jobTitle']", "div.job-title a",
                     "a[href*='/job/']", "a[href*='job-detail']"]:
            title_el = await card.query_selector(sel)
            if title_el:
                break

        title = ""
        url = ""
        if title_el:
            title = (await title_el.inner_text()).strip()
            href = await title_el.get_attribute("href")
            if href:
                url = href if href.startswith("http") else f"https://www.hirist.tech{href}"

        if not url:
            return None

        # Company
        company_el = await card.query_selector(
            "div.company-name, span.company-name, a[class*='company'], "
            "div[class*='company'], span[class*='company']"
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
            "div[class*='exp'], span.exp"
        )
        experience = (await exp_el.inner_text()).strip() if exp_el else ""

        # Snippet
        snippet_el = await card.query_selector(
            "div.job-description, div[class*='snippet'], "
            "div[class*='description'], p[class*='desc']"
        )
        snippet = (await snippet_el.inner_text()).strip()[:200] if snippet_el else ""

        return RawJob(
            url=url,
            title=title,
            company=company,
            location=location,
            experience_required=experience,
            snippet=snippet,
            source="Hirist",
        )

    def _build_search_url(self, keyword: str, location: str, experience: int,
                           page_num: int = 0) -> str:
        """Build Hirist job search URL with date filter and pagination.

        Hirist uses hyphenated keywords in the URL path, similar to Naukri.
        Date filter: jobAge={days}
        Pagination: pageNo=1, 2, 3, ...
        """
        kw_slug = keyword.lower().replace(" ", "-")
        loc_slug = location.lower().replace(" ", "-")
        emin = self.search_config.get("experience_min", experience)
        emax = self.search_config.get("experience_max", experience)
        # Hirist accepts repeated &experience=N like Naukri
        exp_params = "".join(
            f"&experience={y}" for y in range(int(emin), int(emax) + 1)
        )
        url = (
            f"{self.base_url}/{kw_slug}-jobs-in-{loc_slug}"
            f"?experience={experience}"
            f"{exp_params}"
            f"&sort=date"
            f"&jobAge={self.max_age_days}"
        )
        if page_num > 0:
            url += f"&pageNo={page_num + 1}"
        return url

    @staticmethod
    def _extract_job_urls(html: str) -> list[str]:
        """Fallback regex extraction for Hirist job URLs."""
        pattern = r'https://www\.hirist\.tech/job/[^\s"\'<>]+'
        urls = list(set(re.findall(pattern, html)))
        # Also try the hyphenated job listing pattern
        alt_pattern = r'https://www\.hirist\.tech/[a-z0-9-]+-jobs-\d+[^\s"\'<>]*'
        urls.extend(set(re.findall(alt_pattern, html)))
        return list(set(urls))[:25]
