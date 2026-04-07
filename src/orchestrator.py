"""Pipeline orchestrator — runs agents sequentially with metrics tracking."""

import uuid
from datetime import datetime
from typing import Optional

import structlog

from src.agents.base import AgentResult
from src.agents.parsing import ParsingAgent
from src.agents.resume_profiler import ResumeProfiler
from src.agents.sourcing import SourcingAgent
from src.core.browser import browser_context
from src.core.config import load_config
from src.core.db import Database
from src.core.embeddings import EmbeddingModel
from src.core.llm import LLMClient
from src.core.sheets import SheetsWriter
from src.eval.logger import setup_logging
from src.eval.metrics import MetricsCollector

logger = structlog.get_logger()


async def run_pipeline(config: Optional[dict] = None) -> dict:
    """Run the full Phase 1 pipeline: profile → source → parse → sheets.

    Returns a summary dict with metrics.
    """
    if config is None:
        config = load_config()

    setup_logging(config.get("logging", {}).get("level", "INFO"))

    run_id = str(uuid.uuid4())[:8]
    started_at = datetime.utcnow().isoformat()
    metrics = MetricsCollector()
    metrics.start_run()

    logger.info("pipeline_start", run_id=run_id)

    # Initialize shared dependencies
    db = Database(config.get("output", {}).get("db_path", "./data/job_hunt.db"))
    llm = LLMClient(config)
    llm.reset_usage()
    emb = EmbeddingModel(config)

    # Log run to DB
    db.insert_run({"id": run_id, "started_at": started_at, "config_snapshot": {}})

    summary = {
        "run_id": run_id,
        "started_at": started_at,
        "jobs_found": 0,
        "jobs_parsed": 0,
        "errors": 0,
    }

    try:
        # Step 1: Resume profiler (one-time, cached)
        with metrics.track_agent("resume_profiler", items_in=1) as m:
            profiler = ResumeProfiler(config, llm, embedding_model=emb)
            profile_result = await profiler.run()
            m.items_out = 1 if profile_result.success else 0
            if profile_result.errors:
                m.errors.extend(profile_result.errors)

        if not profile_result.success:
            logger.warning("resume_profile_failed", errors=profile_result.errors)
            # Continue without profile — sourcing doesn't need it

        # Step 2: Sourcing (browser scraping)
        async with browser_context(config) as ctx:
            with metrics.track_agent("sourcing") as m:
                sourcing = SourcingAgent(config, db, ctx)
                source_result = await sourcing.run()
                m.items_out = source_result.count
                m.errors.extend(source_result.errors)
                summary["jobs_found"] = source_result.count

            # Step 3: Parsing (visit each URL)
            with metrics.track_agent("parsing", items_in=source_result.count) as m:
                parser = ParsingAgent(config, db, ctx, llm)
                parse_result = await parser.run()
                m.items_out = parse_result.count
                m.items_in = parse_result.count + len(parse_result.errors)
                m.errors.extend(parse_result.errors)
                summary["jobs_parsed"] = parse_result.count

        # Step 4: Write to Google Sheets
        try:
            sheets_config = config.get("sheets", {})
            if sheets_config.get("sheet_id"):
                writer = SheetsWriter(
                    sheets_config["credentials_path"],
                    sheets_config["sheet_id"],
                )
                all_jobs = db.get_jobs()

                # Phase 1: no contacts or drafts yet
                contacts_by_job = {}
                drafts_by_job = {}

                rows_written = writer.write_jobs(all_jobs, contacts_by_job, drafts_by_job)
                logger.info("sheets_updated", rows=rows_written)
            else:
                logger.warning("sheets_skipped", reason="no sheet_id configured")
        except Exception as e:
            logger.error("sheets_failed", error=str(e))
            summary["errors"] += 1

    except Exception as e:
        logger.error("pipeline_failed", error=str(e))
        summary["errors"] += 1

    # Finalize
    metrics.end_run()
    completed_at = datetime.utcnow().isoformat()
    summary["completed_at"] = completed_at
    summary["duration_s"] = round(metrics.total_duration_s, 1)
    summary["errors"] = metrics.total_errors

    # Log usage
    usage = llm.get_usage_summary()
    summary["llm_cost"] = usage["total_cost_usd"]
    summary["llm_calls"] = usage["total_calls"]

    # Update run record in DB
    db.update_run(run_id,
                  completed_at=completed_at,
                  jobs_found=summary["jobs_found"],
                  jobs_parsed=summary["jobs_parsed"],
                  errors=summary["errors"])

    # Log agent metrics to DB
    for am in metrics.agent_metrics:
        db.log_agent_metrics(
            run_id=run_id, agent=am.agent,
            items_in=am.items_in, items_out=am.items_out,
            success_rate=am.success_rate, avg_duration_ms=am.duration_ms,
            errors=am.errors,
        )

    # Write run log to sheets
    try:
        sheets_config = config.get("sheets", {})
        if sheets_config.get("sheet_id"):
            writer = SheetsWriter(
                sheets_config["credentials_path"],
                sheets_config["sheet_id"],
            )
            writer.write_run_log(summary)
    except Exception:
        pass

    db.close()

    logger.info("pipeline_complete", **summary)
    return summary
