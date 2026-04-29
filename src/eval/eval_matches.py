import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import structlog

from src.core.config import load_config
from src.core.db import Database
from src.core.llm import LLMClient

logger = structlog.get_logger()

EVAL_SYSTEM_PROMPT = """You are an expert AI Job Match Evaluator.
Your role is to evaluate a deterministic matching algorithm's performance on job descriptions.

You will be provided with:
1. The candidate's background and configuration.
2. A Job Description (JD).
3. The deterministic scores calculated by the system for this job (e.g. skill, experience, domain, total score).

Your task is to review the job description against the candidate's profile and evaluate whether the system's deterministic scores make sense.
If there are discrepancies (e.g. the system missed a key disqualifier, or gave too high of a domain score for a B2B SaaS role when the candidate is B2C), point them out.

Return your evaluation as a valid JSON object with the following structure:
{
    "proposed_score": 85, // Your subjective rating (0-100) of how well the candidate actually fits this role
    "discrepancies": ["list of areas where the deterministic scoring was too high or too low"],
    "feedback": "A short paragraph explaining your overall judgment.",
    "suggested_tweaks": ["specific actionable advice to improve the matching algorithm (e.g., 'add X to disqualifiers', 'reduce experience weight', etc.)"]
}
"""

async def run_evaluator(limit: int = 10) -> str:
    """Run the LLM evaluator over a sample of scored jobs and generate a report."""
    config = load_config()
    db = Database(config.get("output", {}).get("db_path", "./data/job_hunt.db"))

    llm_config = config.get("llm", {})
    fallback_model = llm_config.get("fallback_model", "openrouter/free")

    # Try to use a "thinking" model or at least the default model for evaluation
    eval_model = llm_config.get("default_model", fallback_model)
    if "agents" in llm_config and "matching" in llm_config["agents"]:
        eval_model = llm_config["agents"]["matching"].get("model", eval_model)

    llm = LLMClient(config)

    # Fetch parsed and scored jobs
    jobs = db.get_jobs(parse_status="parsed")

    # Filter for jobs that have actually been scored (total score > 0)
    scored_jobs = [j for j in jobs if j.get("match_score", 0) > 0]

    if not scored_jobs:
        logger.warning("No scored jobs found in the database. Run the pipeline first.")
        return ""

    # Take a sample
    import random
    sample_jobs = random.sample(scored_jobs, min(limit, len(scored_jobs)))

    logger.info(f"Evaluating {len(sample_jobs)} jobs using {eval_model}...")

    # Load candidate profile for context
    profile_path = Path(config.get("resume", {}).get("profile_cache", "./data/candidate_profile.json"))
    candidate_summary = "Candidate profile not found."
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                profile_data = json.load(f)
                candidate_summary = profile_data.get("summary", "")
        except Exception as e:
            logger.warning("Failed to load candidate profile", error=str(e))

    evaluations = []

    for i, job in enumerate(sample_jobs):
        logger.info(f"Evaluating job {i+1}/{len(sample_jobs)}: {job.get('company')} - {job.get('title')}")

        # Prepare context for the LLM
        jd_text = job.get("full_description") or job.get("jd_summary") or ""

        # We cap JD text to avoid token limits, but keep enough for evaluation
        jd_text_truncated = jd_text[:4000] if jd_text else "No description available."

        prompt = f"""CANDIDATE BACKGROUND:
{candidate_summary}

JOB:
Title: {job.get('title')}
Company: {job.get('company')}
Location: {job.get('location')}

JOB DESCRIPTION (excerpt):
{jd_text_truncated}

DETERMINISTIC SCORES GIVEN BY SYSTEM:
Total Match Score: {job.get('match_score')}%
Skill Score: {job.get('skill_score')}%
Experience Score: {job.get('experience_score')}%
Location Score: {job.get('location_score')}%
Domain Score: {job.get('domain_score')}%
Role Fit Score: {job.get('role_fit_score')}%
Fit Bucket: {job.get('fit_bucket')}
Penalty Reasons: {job.get('penalty_reasons')}

Analyze this match and provide your evaluation in JSON format.
"""

        try:
            # We override the model for this specific call to ensure we use a good one
            eval_result = await llm.complete_json(
                prompt=prompt,
                system=EVAL_SYSTEM_PROMPT,
                agent="evaluator" # custom agent name just for metrics
            )

            evaluations.append({
                "job": job,
                "evaluation": eval_result
            })

        except Exception as e:
            logger.error(f"Failed to evaluate job {job.get('id')}", error=str(e))

    # Generate Markdown Report
    report_path = _generate_markdown_report(evaluations)
    return report_path

def _generate_markdown_report(evaluations: List[Dict[str, Any]]) -> str:
    """Generate a markdown report from the evaluations."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    reports_dir = Path("./eval_reports")
    reports_dir.mkdir(exist_ok=True)

    report_file = reports_dir / f"match_eval_report_{timestamp}.md"

    lines = []
    lines.append(f"# Job Match Evaluation Report")
    lines.append(f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**Jobs Evaluated:** {len(evaluations)}")
    lines.append("\n---\n")

    # Extract global insights
    all_tweaks = []
    for eval_data in evaluations:
        res = eval_data.get("evaluation", {})
        tweaks = res.get("suggested_tweaks", [])
        if isinstance(tweaks, list):
            all_tweaks.extend(tweaks)

    if all_tweaks:
        lines.append("## 🧠 Aggregate Recommendations for Logic/Weight Tweaks")
        for tweak in set(all_tweaks): # deduplicate simple strings
            lines.append(f"- {tweak}")
        lines.append("\n---\n")

    # Detailed Job Breakdowns
    lines.append("## 📊 Detailed Job Evaluations\n")

    for eval_data in evaluations:
        job = eval_data["job"]
        res = eval_data["evaluation"]

        system_score = job.get('match_score', 0)
        llm_score = res.get('proposed_score', 'N/A')

        diff = 0
        if isinstance(llm_score, (int, float)) and isinstance(system_score, (int, float)):
            diff = llm_score - system_score

        diff_str = f"(+{diff:.1f})" if diff > 0 else f"({diff:.1f})"

        lines.append(f"### {job.get('title')} @ {job.get('company')}")
        lines.append(f"- **System Score:** {system_score}% | **Judge Proposed Score:** {llm_score}% {diff_str}")
        lines.append(f"- **System Fit Bucket:** {job.get('fit_bucket')}")
        lines.append(f"\n**Feedback:**\n{res.get('feedback', 'No feedback provided.')}\n")

        discrepancies = res.get('discrepancies', [])
        if discrepancies:
            lines.append("**Discrepancies Noted:**")
            for d in discrepancies:
                lines.append(f"- {d}")
        lines.append("\n")

    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"Saved evaluation report to {report_file}")
    return str(report_file)

if __name__ == "__main__":
    asyncio.run(run_evaluator())
