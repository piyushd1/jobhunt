"""Sourcing Agent — discovers jobs across all enabled portals.

Runs each portal adapter, deduplicates by fingerprint, merges sources
when the same job appears on multiple portals, and writes to SQLite.
"""

import uuid
from typing import Any

from playwright.async_api import BrowserContext

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.browser import new_page
from src.core.config import get_enabled_portals
from src.core.db import Database
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

        # Deduplicate and write to DB
        new_count = 0
        merged_count = 0

        for raw_job in all_jobs:
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
                    "experience_required": raw_job.experience_required,
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
            metadata={"total_scraped": len(all_jobs)},
        )
