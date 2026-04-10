"""Sourcing Agent — discovers jobs across all enabled portals.

Runs each portal adapter, deduplicates by fingerprint, merges sources
when the same job appears on multiple portals, and writes to SQLite.
"""

import re
import uuid
from typing import Any

from playwright.async_api import BrowserContext

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.browser import new_page
from src.core.config import get_enabled_portals
from src.core.db import Database
from src.core.roles import (
    DEFAULT_ALLOWED_ROLE_FAMILIES,
    DEFAULT_EXCLUDED_TITLE_KEYWORDS,
    classify_role_family,
    is_allowed_role,
)
from src.portals import get_adapter
from src.portals.base import RawJob

logger = structlog.get_logger()


class SourcingAgent(BaseAgent):
    """Find job listings across all enabled portals."""

    name = "sourcing"

    def __init__(self, config: dict, db: Database, browser_ctx: BrowserContext):
        super().__init__(config)
        self.db = db
        self.browser_ctx = browser_ctx
        search_config = config.get("search", {})
        self.allowed_role_families = search_config.get(
            "allowed_role_families", DEFAULT_ALLOWED_ROLE_FAMILIES
        )
        self.excluded_title_keywords = search_config.get(
            "excluded_title_keywords", DEFAULT_EXCLUDED_TITLE_KEYWORDS
        )

    async def run(self, input_data: Any = None) -> AgentResult:
        """Scrape all enabled portals and deduplicate into SQLite."""
        enabled = get_enabled_portals(self.config)
        if not enabled:
            return AgentResult(errors=["No portals enabled in config"])

        all_jobs: list[RawJob] = []
        errors: list[str] = []
        portal_stats: dict[str, int] = {}

        for portal_name in enabled:
            try:
                adapter = get_adapter(portal_name, self.config)
                page = await new_page(self.browser_ctx)

                # Health check first
                health = await adapter.health_check(page)
                if health["status"] == "down":
                    errors.append(f"{portal_name}: down — {health['details']}")
                    await page.close()
                    continue
                elif health["status"] == "degraded":
                    logger.warning("portal_degraded", portal=portal_name, details=health["details"])

                # Scrape
                logger.info("sourcing_portal_start", portal=portal_name)
                jobs = await adapter.scrape(page)
                all_jobs.extend(jobs)
                portal_stats[portal_name] = len(jobs)
                logger.info("sourcing_portal_done", portal=portal_name, found=len(jobs))

                await page.close()

            except Exception as e:
                errors.append(f"{portal_name}: {str(e)}")
                logger.error("sourcing_portal_error", portal=portal_name, error=str(e))

        # Apply blacklist filter
        blacklist_config = self.config.get("blacklist", {})
        blocked_companies = set(c.lower() for c in blacklist_config.get("companies", []))
        blocked_keywords = set(k.lower() for k in blacklist_config.get("title_keywords", []))

        # Also load blacklist from DB
        for bl in self.db.get_blacklist():
            if bl["type"] == "company":
                blocked_companies.add(bl["value"].lower())
            elif bl["type"] == "title_keyword":
                blocked_keywords.add(bl["value"].lower())

        # Experience filter from config
        search_config = self.config.get("search", {})
        exp_min = search_config.get("experience_min", 0)
        exp_max = search_config.get("experience_max", 99)
        exp_buffer = search_config.get("experience_buffer", 2)
        # Effective range: (min - buffer) to (max + buffer)
        effective_min = max(0, exp_min - exp_buffer)
        effective_max = exp_max + exp_buffer

        filtered_jobs = []
        blocked_count = 0
        exp_filtered_count = 0
        role_filtered_count = 0
        for job in all_jobs:
            company_lower = (job.company or "").lower()
            title_lower = (job.title or "").lower()
            if any(bc in company_lower for bc in blocked_companies if bc):
                blocked_count += 1
                continue
            if any(bk in title_lower for bk in blocked_keywords if bk):
                blocked_count += 1
                continue
            # Filter by experience range (if experience info is available)
            if job.experience_required:
                if not _experience_in_range(job.experience_required, effective_min, effective_max):
                    exp_filtered_count += 1
                    continue
            if not is_allowed_role(
                job.title,
                description=job.snippet,
                allowed_families=self.allowed_role_families,
                excluded_keywords=self.excluded_title_keywords,
            ):
                role_filtered_count += 1
                continue
            filtered_jobs.append(job)

        if blocked_count:
            logger.info("sourcing_blacklist_filtered", blocked=blocked_count)
        if exp_filtered_count:
            logger.info("sourcing_experience_filtered", filtered=exp_filtered_count,
                         range=f"{effective_min}-{effective_max} years")
        if role_filtered_count:
            logger.info("sourcing_role_filtered", filtered=role_filtered_count,
                        allowed=self.allowed_role_families)

        # Deduplicate and write to DB
        new_count = 0
        merged_count = 0

        for raw_job in filtered_jobs:
            fp = raw_job.fingerprint

            if self.db.job_exists(fp):
                # Job already exists — merge the new source
                self.db.merge_job_source(fp, raw_job.source, raw_job.url)
                merged_count += 1
            else:
                # New job — insert
                job_record = {
                    "id": str(uuid.uuid4()),
                    "fingerprint": fp,
                    "source": raw_job.source,
                    "source_urls": {raw_job.source: raw_job.url},
                    "url": raw_job.url,
                    "title": raw_job.title,
                    "company": raw_job.company,
                    "location": raw_job.location,
                    "remote": raw_job.remote,
                    "snippet": raw_job.snippet,
                    "posted_date": raw_job.posted_date,
                    "experience_required": raw_job.experience_required,
                    "role_family_hint": classify_role_family(raw_job.title, raw_job.snippet),
                    "status": "new",
                    "parse_status": "pending",
                }
                if self.db.insert_job(job_record):
                    new_count += 1

        logger.info("sourcing_complete",
                     total_scraped=len(all_jobs),
                     new_jobs=new_count,
                     merged=merged_count,
                     portals=portal_stats)

        return AgentResult(
            data={"new_jobs": new_count, "merged": merged_count, "portal_stats": portal_stats},
            count=new_count,
            errors=errors,
            metadata={
                "total_scraped": len(all_jobs),
                "exp_filtered": exp_filtered_count,
                "role_filtered": role_filtered_count,
            },
        )


def _experience_in_range(exp_str: str, min_years: int, max_years: int) -> bool:
    """Check if a job's experience requirement overlaps with the target range.

    Handles formats like: "5-8 years", "3+ years", "7 years", "5 to 10 yrs",
    "Senior (8-12 years)", "Minimum 5 years".

    A job qualifies if ANY part of its range overlaps with [min_years, max_years].
    E.g., "5-8 years" overlaps with 7-13 range → True
    E.g., "1-3 years" does NOT overlap with 5-15 range → False
    """
    if not exp_str:
        return True  # Unknown experience → don't filter out

    # Try to extract numbers from the experience string
    numbers = re.findall(r'(\d+)', exp_str)
    if not numbers:
        return True  # Can't parse → don't filter out

    nums = [int(n) for n in numbers if int(n) <= 50]  # Ignore numbers that aren't years
    if not nums:
        return True

    if len(nums) >= 2:
        # Range like "5-8 years" or "3 to 7 years"
        job_min = min(nums[:2])
        job_max = max(nums[:2])
    else:
        # Single number like "5+ years" or "minimum 5 years"
        job_min = nums[0]
        job_max = nums[0] + 2  # Assume a 2-year band above stated minimum

    # Check if ranges overlap: [job_min, job_max] ∩ [min_years, max_years]
    return job_min <= max_years and job_max >= min_years
