"""LinkedIn Posts portal — find 'hiring' posts written by people.

Different from the regular LinkedIn Jobs portal: this scrapes the
content/posts search and treats each hiring post as a job-with-direct-contact.

The author of the post IS the contact — usually a hiring manager,
recruiter, or founder posting "we're hiring [role]". Reach-out is
direct (LinkedIn DM) rather than via a company People search.
"""

import re
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import Page

import structlog

from src.core.browser import human_delay, random_scroll
from src.portals.base import PortalAdapter, RawJob

logger = structlog.get_logger()


class LinkedInPostsAdapter(PortalAdapter):
    """Scrape LinkedIn content (posts) for hiring announcements."""

    name = "linkedin_posts"
    base_url = "https://www.linkedin.com/search/results/content/"

    # Boolean phrases used to find hiring posts. Each combines a hiring
    # signal with a role keyword from the candidate's target list.
    HIRING_PHRASES = [
        "hiring",
        "we're hiring",
        "we are hiring",
        "looking to hire",
        "open role",
        "open position",
        "join our team",
    ]

    async def scrape(self, page: Page) -> list[RawJob]:
        keywords = self.search_config.get("keywords", [])
        locations = self.get_locations()
        jobs: list[RawJob] = []
        seen_post_ids: set[str] = set()

        # For each keyword, search hiring + keyword
        for keyword in keywords:
            if len(jobs) >= self.max_results:
                break

            for hiring_phrase in self.HIRING_PHRASES[:3]:  # cap to top 3 phrases
                if len(jobs) >= self.max_results:
                    break

                query = f'"{hiring_phrase}" {keyword}'
                url = self._build_search_url(query)
                logger.info("linkedin_posts_searching",
                            phrase=hiring_phrase, keyword=keyword,
                            total_so_far=len(jobs))

                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await human_delay(2, 4)
                    # Posts use infinite scroll — scroll a couple of times.
                    for _ in range(self.pages_per_search):
                        await random_scroll(page, scrolls=3)
                        await human_delay(2, 3)

                    page_jobs = await self._extract_posts(page, hiring_phrase, keyword)
                    new_posts = [j for j in page_jobs if j.url not in seen_post_ids]
                    if not new_posts:
                        continue

                    for j in new_posts:
                        seen_post_ids.add(j.url)
                    jobs.extend(new_posts)
                    logger.info("linkedin_posts_query_done",
                                phrase=hiring_phrase, keyword=keyword,
                                found=len(new_posts))
                except Exception as e:
                    logger.warning("linkedin_posts_search_failed",
                                    phrase=hiring_phrase, keyword=keyword,
                                    error=str(e))
                    continue

                await human_delay(3, 5)

        # Final dedup by URL — already done above, but double-safety
        seen = set()
        unique = []
        for j in jobs:
            if j.url not in seen:
                seen.add(j.url)
                unique.append(j)
        return unique[:self.max_results]

    async def _extract_posts(self, page: Page, hiring_phrase: str,
                              keyword: str) -> list[RawJob]:
        """Extract hiring posts from a content-search results page.

        Each post becomes a RawJob with:
          - url: the post permalink
          - title: inferred role (the keyword we searched for)
          - company: empty (often hidden in posts; LLM parsing fills it later)
          - snippet: the post text (first ~500 chars)
        """
        jobs: list[RawJob] = []

        # Multiple selector strategies for post containers (LinkedIn DOM changes often)
        post_selectors = [
            "div.feed-shared-update-v2",
            "div[data-urn*='urn:li:activity:']",
            "li.reusable-search__result-container",
            "div.update-components-text",
            "article",
        ]
        posts = []
        for sel in post_selectors:
            try:
                found = await page.query_selector_all(sel)
                if found:
                    posts = found
                    logger.debug("linkedin_posts_selector_hit",
                                 selector=sel, count=len(found))
                    break
            except Exception:
                continue

        for post_el in posts[:25]:
            try:
                job = await self._parse_post(post_el, hiring_phrase, keyword)
                if job:
                    jobs.append(job)
            except Exception:
                continue

        # Fallback: scan all activity links from raw HTML if selectors miss
        if not jobs:
            try:
                content = await page.content()
                urls = self._extract_post_urls(content)
                for u in urls[:15]:
                    jobs.append(RawJob(
                        url=u, title=keyword,
                        snippet=f"hiring post mentioning {keyword}",
                        source="LinkedIn Posts",
                    ))
            except Exception:
                pass

        return jobs

    async def _parse_post(self, post_el, hiring_phrase: str,
                          keyword: str) -> Optional[RawJob]:
        """Pull URL + author + first snippet from a post element."""
        # 1. Post permalink: look for a link to /posts/ or /feed/update/
        link_el = None
        for link_selector in (
            "a[href*='/posts/']",
            "a[href*='/feed/update/']",
            "a[href*='/activity-']",
            "a.app-aware-link[href*='/in/']",  # falls back to author profile
        ):
            try:
                link_el = await post_el.query_selector(link_selector)
                if link_el:
                    break
            except Exception:
                continue

        if not link_el:
            return None

        try:
            href = await link_el.get_attribute("href")
        except Exception:
            href = None
        if not href:
            return None

        url = href.split("?")[0]
        if not url.startswith("http"):
            url = "https://www.linkedin.com" + url

        # 2. Author name (likely the hiring contact)
        author = ""
        for author_selector in (
            "span.update-components-actor__name span[aria-hidden='true']",
            "span.update-components-actor__title span",
            "span.feed-shared-actor__name span[aria-hidden='true']",
            "a.app-aware-link span[dir='ltr']",
            "span[aria-hidden='true']",
        ):
            try:
                el = await post_el.query_selector(author_selector)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and len(text) > 1 and "ago" not in text.lower():
                        author = text
                        break
            except Exception:
                continue

        # 3. Post text (snippet)
        snippet = ""
        for text_selector in (
            "div.update-components-text",
            "div.feed-shared-update-v2__description",
            "span.break-words",
            "div.feed-shared-text",
        ):
            try:
                el = await post_el.query_selector(text_selector)
                if el:
                    snippet = (await el.inner_text()).strip()[:500]
                    break
            except Exception:
                continue

        # Sanity check: if neither hiring phrase nor keyword appears in the
        # snippet, the post probably isn't actually a hiring post.
        haystack = f"{snippet} {author}".lower()
        if hiring_phrase.lower() not in haystack and keyword.lower() not in haystack:
            return None

        return RawJob(
            url=url,
            title=keyword,                       # role keyword we searched for
            company=author or "",                # author = likely hiring contact
            snippet=snippet,
            source="LinkedIn Posts",
        )

    def _build_search_url(self, query: str) -> str:
        """Build a content-search URL with date filter."""
        # datePosted: "past-week" | "past-month" | "past-24h"
        if self.max_age_days <= 1:
            date_param = "past-24h"
        elif self.max_age_days <= 7:
            date_param = "past-week"
        else:
            date_param = "past-month"
        encoded_query = quote_plus(query)
        return (
            f"{self.base_url}?keywords={encoded_query}"
            f"&datePosted=%22{date_param}%22"
            f"&sortBy=date_posted"
        )

    @staticmethod
    def _extract_post_urls(html: str) -> list[str]:
        """Fallback regex extraction for LinkedIn post URLs."""
        patterns = [
            r"https://www\.linkedin\.com/posts/[^\s\"'<>]+",
            r"https://www\.linkedin\.com/feed/update/[^\s\"'<>]+",
        ]
        urls: list[str] = []
        for p in patterns:
            urls.extend(re.findall(p, html))
        # Dedup and clean
        seen = set()
        cleaned = []
        for u in urls:
            u = u.split("?")[0]
            if u not in seen:
                seen.add(u)
                cleaned.append(u)
        return cleaned[:30]
