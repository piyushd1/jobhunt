"""Naukri.com portal adapter.

Scrapes Naukri's job search results using a logged-in persistent session.
"""

import re
from urllib.parse import quote_plus

from typing import Optional

from playwright.async_api import Page

import structlog

from src.core.browser import human_delay, random_scroll
from src.portals.base import PortalAdapter, RawJob

logger = structlog.get_logger()


class NaukriAdapter(PortalAdapter):
    """Scrape Naukri.com job search results."""

    name = "naukri"
    base_url = "https://www.naukri.com"

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        location = self.search_config.get("location", "India")
        experience = self.search_config.get("experience_years", 5)
        jobs: list[RawJob] = []

        for keyword in keywords:
            if len(jobs) >= self.max_results:
                break

            url = self._build_search_url(keyword, location, experience)
            logger.info("naukri_searching", keyword=keyword, location=location)

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await human_delay(2, 4)
                await random_scroll(page, scrolls=2)

                page_jobs = await self._extract_jobs(page)
                jobs.extend(page_jobs)
                logger.info("naukri_keyword_done", keyword=keyword, found=len(page_jobs))

            except Exception as e:
                logger.warning("naukri_search_failed", keyword=keyword, error=str(e))
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
        """Extract job cards from Naukri search results."""
        jobs = []

        try:
            # Naukri job cards
            cards = await page.query_selector_all(
                "article.jobTuple, div.srp-jobtuple-wrapper, "
                "div.cust-job-tuple, div[data-job-id]"
            )

            for card in cards:
                try:
                    job = await self._parse_card(card)
                    if job:
                        jobs.append(job)
                except Exception:
                    continue

        except Exception as e:
            logger.warning("naukri_selector_failed", error=str(e))

        # Fallback: regex URL extraction
        if not jobs:
            logger.info("naukri_using_url_fallback")
            content = await page.content()
            urls = self._extract_job_urls(content)
            for url in urls:
                jobs.append(RawJob(url=url, source="Naukri"))

        return jobs

    async def _parse_card(self, card) -> Optional[RawJob]:
        """Parse a single Naukri job card."""
        # Title + URL
        title_el = await card.query_selector(
            "a.title, a.cust-job-tuple-title, "
            "a[class*='title'], h2 a"
        )
        title = ""
        url = ""
        if title_el:
            title = (await title_el.inner_text()).strip()
            href = await title_el.get_attribute("href")
            if href:
                url = href if href.startswith("http") else f"https://www.naukri.com{href}"

        if not url:
            return None

        # Company
        company_el = await card.query_selector(
            "a.comp-name, span.comp-name, a[class*='company'], "
            "a.subTitle, span.comp-dtls-wrap a"
        )
        company = (await company_el.inner_text()).strip() if company_el else ""

        # Location
        loc_el = await card.query_selector(
            "span.loc, span.locWdth, span[class*='location'], "
            "li.location, span.ni-job-tuple-icon-srp-location"
        )
        location = (await loc_el.inner_text()).strip() if loc_el else ""

        # Experience
        exp_el = await card.query_selector(
            "span.exp, span.expwdth, span[class*='experience'], "
            "li.experience, span.ni-job-tuple-icon-srp-experience"
        )
        experience = (await exp_el.inner_text()).strip() if exp_el else ""

        return RawJob(
            url=url,
            title=title,
            company=company,
            location=location,
            experience_required=experience,
            source="Naukri",
        )

    def _build_search_url(self, keyword: str, location: str, experience: int) -> str:
        """Build Naukri job search URL."""
        # Naukri uses hyphenated keywords in URL
        kw_slug = keyword.lower().replace(" ", "-")
        loc_slug = location.lower().replace(" ", "-")
        return (
            f"{self.base_url}/{kw_slug}-jobs-in-{loc_slug}"
            f"?experience={experience}"
        )

    @staticmethod
    def _extract_job_urls(html: str) -> list[str]:
        """Fallback regex extraction for Naukri job URLs."""
        pattern = r'https://www\.naukri\.com/job-listings-[^\s"\'<>]+'
        urls = list(set(re.findall(pattern, html)))
        return urls[:25]
