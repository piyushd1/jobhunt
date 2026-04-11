"""Pipeline orchestrator — runs agents sequentially with live progress and metrics."""

import sys
import uuid
from datetime import datetime
from typing import Optional

import structlog

from src.agents.base import AgentResult
from src.agents.leadgen import LeadGenAgent
from src.agents.matching import MatchingAgent
from src.agents.messaging import MessagingAgent
from src.agents.parsing import ParsingAgent
from src.agents.resume_profiler import ResumeProfiler
from src.agents.sourcing import SourcingAgent
from src.core.browser import browser_context
from src.core.config import load_config
from src.core.skills import SkillCanonicalizer
from src.core.vectorstore import ResumeVectorStore
from src.core.db import Database
from src.core.embeddings import EmbeddingModel
from src.core.llm import LLMClient
from src.core.sheets import SheetsWriter
from src.eval.logger import setup_logging
from src.eval.metrics import MetricsCollector
from src.eval.progress import PipelineProgress, print_summary

logger = structlog.get_logger()

# Force unbuffered output so logs appear in real-time
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None


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

    # Live progress display
    progress = PipelineProgress()
    progress.start(run_id)

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
        # ── Step 1: Resume profiler (one-time, cached) ──
        progress.start_stage("Resume Profile", total=1)
        with metrics.track_agent("resume_profiler", items_in=1) as m:
            profiler = ResumeProfiler(config, llm, embedding_model=emb)
            profile_result = await profiler.run()
            m.items_out = 1 if profile_result.success else 0
            if profile_result.errors:
                m.errors.extend(profile_result.errors)

        candidate_profile = {}
        if profile_result.success:
            candidate_profile = profile_result.data
            skills_count = len(candidate_profile.get("all_skills_canonical", []))
            progress.complete_stage(done=1, errors=0)
            progress.update_stage(detail=f"{skills_count} skills extracted")

            # Index resume into vector store for RAG
            vectorstore = ResumeVectorStore(config, emb)
            stories_file = config.get("matching", {}).get("stories_file", "")
            vectorstore.index_profile(candidate_profile, stories_file=stories_file)
        else:
            progress.fail_stage(", ".join(profile_result.errors))
            vectorstore = None

        # ── Step 2: Sourcing (browser scraping) ──
        progress.start_stage("Sourcing Jobs")

        async with browser_context(config) as ctx:
            with metrics.track_agent("sourcing") as m:
                sourcing = SourcingAgent(config, db, ctx)

                # Patch sourcing to report portal progress
                _original_run = sourcing.run
                portal_count = [0]

                async def _sourcing_with_progress(*args, **kwargs):
                    result = await _original_run(*args, **kwargs)
                    return result

                source_result = await sourcing.run()
                m.items_out = source_result.count
                m.errors.extend(source_result.errors)
                summary["jobs_found"] = source_result.count

            portal_stats = source_result.data.get("portal_stats", {}) if source_result.data else {}
            portal_detail = ", ".join(f"{k}:{v}" for k, v in portal_stats.items())
            progress.complete_stage(done=source_result.count, errors=len(source_result.errors))
            progress.update_stage(detail=portal_detail)

            # ── Step 3: Parsing (visit each URL, extract JDs) ──
            pending_jobs = db.get_jobs(parse_status="pending")
            total_to_parse = len(pending_jobs)
            progress.start_stage("Parsing JDs", total=total_to_parse)

            with metrics.track_agent("parsing", items_in=total_to_parse) as m:
                parser = ParsingAgent(config, db, ctx, llm)

                # Hook into the parser to get per-job progress
                _original_parse = parser.run

                async def _parsing_with_progress():
                    """Run parsing with live progress updates."""
                    from src.agents.base import AgentResult
                    pending = db.get_jobs(parse_status="pending")
                    if not pending:
                        return AgentResult(data=[], count=0)

                    from src.core.browser import new_page, safe_goto, human_delay
                    import asyncio

                    page = await new_page(parser.browser_ctx)
                    parsed_count = 0
                    errors = []

                    for i, job in enumerate(pending):
                        try:
                            result = await parser._parse_job(page, job)
                            if result:
                                db.update_job(job["id"], **result, parse_status="parsed")
                                parsed_count += 1
                                title = job.get("title", "Unknown")[:40]
                                company = job.get("company", "")[:20]
                                progress.update_stage(
                                    done=i + 1,
                                    errors=len(errors),
                                    detail=f"{title} @ {company}",
                                )
                            else:
                                db.update_job(job["id"], parse_status="failed")
                                errors.append(f"No content: {job['url']}")
                                progress.update_stage(done=i + 1, errors=len(errors))
                        except Exception as e:
                            db.update_job(job["id"], parse_status="failed")
                            errors.append(f"{job['url']}: {str(e)}")
                            progress.update_stage(done=i + 1, errors=len(errors))

                        await asyncio.sleep(parser.delay_between_calls)
                        await human_delay(1, 2)

                    await page.close()
                    return AgentResult(
                        data={"parsed": parsed_count, "failed": len(errors)},
                        count=parsed_count,
                        errors=errors,
                    )

                parse_result = await _parsing_with_progress()
                m.items_out = parse_result.count
                m.items_in = total_to_parse
                m.errors.extend(parse_result.errors)
                summary["jobs_parsed"] = parse_result.count

            progress.complete_stage(done=parse_result.count, errors=len(parse_result.errors))

        # ── Step 4: Matching (score jobs against resume) ──
        if candidate_profile and vectorstore:
            unscored = db.get_jobs(parse_status="parsed")
            unscored = [j for j in unscored if j.get("match_score") is None]

            if unscored:
                progress.start_stage("Matching", total=len(unscored))
                canonicalizer = SkillCanonicalizer(embedding_model=emb, similarity_threshold=0.55)

                with metrics.track_agent("matching", items_in=len(unscored)) as m:
                    matcher = MatchingAgent(
                        config, db, llm, vectorstore, canonicalizer, candidate_profile
                    )

                    # Run matching with progress
                    scored = 0
                    match_errors = []
                    for i, job in enumerate(unscored):
                        try:
                            role_family = matcher._resolve_role_family(job)
                            if role_family not in matcher.allowed_role_families:
                                matcher._mark_out_of_scope(job, role_family)
                                detail = f"weak_fit — {job.get('title', '')[:35]}"
                            else:
                                scores = matcher._compute_deterministic_score(job, role_family=role_family)
                                llm_analysis = await matcher._llm_enhance(job, scores)
                                db.update_job(job["id"],
                                    match_score=scores["total"],
                                    skill_score=scores["skill_score"],
                                    required_skill_score=scores["required_skill_score"],
                                    preferred_skill_score=scores["preferred_skill_score"],
                                    experience_score=scores["experience_score"],
                                    location_score=scores["location_score"],
                                    domain_score=scores["domain_score"],
                                    role_fit_score=scores["role_fit_score"],
                                    matched_skills=scores["matched_skills"],
                                    missing_skills=scores["missing_skills"],
                                    role_family=role_family,
                                    fit_bucket=scores["fit_bucket"],
                                    penalty_reasons=scores["penalty_reasons"],
                                    match_summary=llm_analysis.get("match_summary", scores["fallback_summary"]),
                                )
                                detail = f"{scores['fit_bucket']} {scores['total']}% — {job.get('title', '')[:35]}"
                            scored += 1
                            progress.update_stage(
                                done=i + 1,
                                detail=detail,
                            )
                        except Exception as e:
                            match_errors.append(str(e))
                            progress.update_stage(done=i + 1, errors=len(match_errors))

                        import asyncio
                        await asyncio.sleep(config.get("llm", {}).get("delay_between_calls_s", 2))

                    m.items_out = scored
                    m.errors.extend(match_errors)
                    summary["jobs_scored"] = scored

                progress.complete_stage(done=scored, errors=len(match_errors))
            else:
                logger.info("matching_all_already_scored")
        else:
            logger.warning("matching_skipped", reason="no profile or vectorstore")

        # ── Step 5: Lead Gen (find contacts at shortlisted companies) ──
        async with browser_context(config) as ctx:
            shortlisted = db.get_jobs(parse_status="parsed",
                                      min_score=config.get("matching", {}).get("shortlist_threshold", 70))
            jobs_needing_contacts = [j for j in shortlisted if not db.get_contacts_for_job(j["id"])]

            if jobs_needing_contacts:
                progress.start_stage("Lead Gen", total=len(jobs_needing_contacts))
                with metrics.track_agent("leadgen", items_in=len(jobs_needing_contacts)) as m:
                    leadgen = LeadGenAgent(config, db, ctx)
                    leadgen_result = await leadgen.run()
                    m.items_out = leadgen_result.count
                    m.errors.extend(leadgen_result.errors)
                    summary["contacts_found"] = leadgen_result.count
                    jobs_processed = leadgen_result.data.get("jobs_processed", 0) if leadgen_result.data else 0
                progress.complete_stage(done=jobs_processed, errors=len(leadgen_result.errors))
                progress.update_stage(detail=f"{leadgen_result.count} contacts from {jobs_processed} companies")
            else:
                logger.info("leadgen_skipped", reason="no new shortlisted jobs need contacts")

        # ── Step 6: Messaging (draft outreach for contacts) ──
        if candidate_profile and vectorstore:
            # Check if there are contacts without drafts
            all_contacts_count = db.get_contacts_without_drafts_count(parse_status="parsed")

            if all_contacts_count > 0:
                progress.start_stage("Drafting Messages", total=all_contacts_count)
                with metrics.track_agent("messaging", items_in=all_contacts_count) as m:
                    messenger = MessagingAgent(config, db, llm, vectorstore, candidate_profile)
                    msg_result = await messenger.run()
                    m.items_out = msg_result.count
                    m.errors.extend(msg_result.errors)
                    summary["drafts_created"] = msg_result.count
                progress.complete_stage(done=msg_result.count, errors=len(msg_result.errors))
            else:
                logger.info("messaging_skipped", reason="no contacts need drafts")

        # ── Step 7: Write to Google Sheets (full data now) ──
        progress.start_stage("Google Sheets", total=1)
        try:
            sheets_config = config.get("sheets", {})
            if sheets_config.get("sheet_id"):
                writer = SheetsWriter(
                    sheets_config["credentials_path"],
                    sheets_config["sheet_id"],
                )
                all_jobs = db.get_jobs()

                # Filter: only write well-matched jobs to the sheet
                sheet_min = config.get("matching", {}).get("sheet_min_score", 0)
                if sheet_min > 0:
                    before_count = len(all_jobs)
                    all_jobs = [j for j in all_jobs
                                if j.get("match_score") is not None and j["match_score"] >= sheet_min]
                    logger.info("sheet_filter_applied", min_score=sheet_min,
                                before=before_count, after=len(all_jobs))

                # Build contacts and drafts lookup
                contacts_by_job = {}
                drafts_by_job = {}
                for job in all_jobs:
                    contacts_by_job[job["id"]] = db.get_contacts_for_job(job["id"])
                    drafts_by_job[job["id"]] = db.get_drafts_for_job(job["id"])

                rows_written = writer.write_jobs(all_jobs, contacts_by_job, drafts_by_job)
                progress.complete_stage(done=rows_written)
                progress.update_stage(detail=f"{rows_written} rows written")
            else:
                progress.complete_stage(done=0)
                progress.update_stage(detail="skipped — no sheet_id")
        except Exception as e:
            progress.fail_stage(str(e)[:60])
            summary["errors"] += 1

    except Exception as e:
        logger.error("pipeline_failed", error=str(e))
        progress.fail_stage(str(e)[:60])
        summary["errors"] += 1

    # Stop live display
    progress.stop()

    # Finalize metrics
    metrics.end_run()
    completed_at = datetime.utcnow().isoformat()
    summary["completed_at"] = completed_at
    summary["duration_s"] = round(metrics.total_duration_s, 1)
    summary["errors"] = metrics.total_errors

    # Log usage
    usage = llm.get_usage_summary()
    summary["llm_cost"] = usage["total_cost_usd"]
    summary["llm_calls"] = usage["total_calls"]

    # Print final summary table
    print_summary(summary)

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
