"""Matching Agent — scores jobs against candidate profile using hybrid approach.

Scoring formula:
  Match% = 100 * (0.60 * SkillOverlap + 0.25 * ExperienceFit + 0.15 * LocationFit)

Then an LLM enhancement layer adds qualitative context:
  - Relevant projects from resume (via ChromaDB RAG)
  - Skill gap analysis
  - Match summary explaining the score

Mandatory skill cap: if 2+ required skills are completely missing, cap at 65%.
"""

import json
from typing import Any, Optional

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.db import Database
from src.core.llm import LLMClient
from src.core.skills import SkillCanonicalizer, canonicalize_skill
from src.core.vectorstore import ResumeVectorStore

logger = structlog.get_logger()

MATCH_SYSTEM_PROMPT = """You are a job matching analyst for Product Management roles.

Given a candidate profile summary, their relevant experience chunks, and a job description,
provide a qualitative match analysis.

Return ONLY valid JSON:
{
  "match_summary": "2-3 sentence explanation of why this is/isn't a good fit",
  "matched_projects": ["project/experience that directly relates to this JD"],
  "missing_skills_guidance": ["skill gap: how the candidate could bridge it"],
  "role_fit": "strong_fit / moderate_fit / weak_fit / stretch",
  "key_strengths": ["top 3 reasons this candidate fits"],
  "concerns": ["top concerns about fit"]
}

Be honest and specific. Reference actual skills and experience from the profile."""


class MatchingAgent(BaseAgent):
    """Score and rank jobs against the candidate profile."""

    name = "matching"

    def __init__(self, config: dict, db: Database, llm: LLMClient,
                 vectorstore: ResumeVectorStore, canonicalizer: SkillCanonicalizer,
                 profile: dict):
        super().__init__(config)
        self.db = db
        self.llm = llm
        self.vectorstore = vectorstore
        self.canonicalizer = canonicalizer
        self.profile = profile

        match_config = config.get("matching", {})
        self.weights = match_config.get("weights", {
            "skills": 0.50, "experience": 0.20, "location": 0.15, "domain": 0.15
        })
        self.mandatory_cap = match_config.get("mandatory_skill_cap", 65)

        # Role priority tiers from config
        role_priority = config.get("search", {}).get("role_priority", {})
        self.tier1_roles = [r.lower() for r in role_priority.get("tier1", [])]
        self.tier2_roles = [r.lower() for r in role_priority.get("tier2", [])]
        self.tier3_roles = [r.lower() for r in role_priority.get("tier3", [])]

        # Domain preferences
        domain_prefs = match_config.get("domain_preferences", {})
        self.strong_domains = [d.lower() for d in domain_prefs.get("strong_fit", [])]
        self.moderate_domains = [d.lower() for d in domain_prefs.get("moderate_fit", [])]
        self.weak_domains = [d.lower() for d in domain_prefs.get("weak_fit", [])]

        # Candidate data
        self.candidate_skills = set(profile.get("all_skills_canonical", []))
        self.candidate_years = profile.get("total_experience_years", 0)
        self.candidate_locations = set(
            loc.lower() for loc in profile.get("preferred_locations", [])
        )
        self.candidate_skill_years = profile.get("skill_years", {})

    async def run(self, input_data: Any = None) -> AgentResult:
        """Score all parsed jobs that don't have a match_score yet."""
        unscored = self.db.get_jobs(parse_status="parsed")
        unscored = [j for j in unscored if j.get("match_score") is None]

        if not unscored:
            logger.info("matching_nothing_to_score")
            return AgentResult(data=[], count=0)

        logger.info("matching_start", jobs=len(unscored))
        scored_count = 0
        errors: list[str] = []

        for i, job in enumerate(unscored):
            try:
                scores = self._compute_deterministic_score(job)
                llm_analysis = await self._llm_enhance(job, scores)

                # Save to DB
                self.db.update_job(job["id"],
                    match_score=scores["total"],
                    skill_score=scores["skill_score"],
                    experience_score=scores["experience_score"],
                    location_score=scores["location_score"],
                    matched_skills=scores["matched_skills"],
                    missing_skills=scores["missing_skills"],
                    match_summary=llm_analysis.get("match_summary", ""),
                )
                scored_count += 1

                logger.info("matching_job_scored",
                    progress=f"{i+1}/{len(unscored)}",
                    title=job.get("title", "")[:40],
                    score=scores["total"],
                    fit=llm_analysis.get("role_fit", "unknown"),
                )

            except Exception as e:
                errors.append(f"{job.get('title', 'unknown')}: {str(e)}")
                logger.warning("matching_job_failed", job_id=job["id"], error=str(e))

        logger.info("matching_complete", scored=scored_count, errors=len(errors))
        return AgentResult(
            data={"scored": scored_count},
            count=scored_count,
            errors=errors,
        )

    def _compute_deterministic_score(self, job: dict) -> dict:
        """Compute the weighted formula score."""
        # Parse JD skills
        jd_skills_raw = json.loads(job.get("skills_required", "[]"))
        jd_skills_canonical = []
        for raw in jd_skills_raw:
            canonical, _ = self.canonicalizer.canonicalize(raw)
            jd_skills_canonical.append(canonical)
        jd_skills_set = set(jd_skills_canonical)

        # Skill overlap
        matched = self.candidate_skills & jd_skills_set
        missing = jd_skills_set - self.candidate_skills

        if len(jd_skills_set) > 0:
            skill_score = len(matched) / len(jd_skills_set)
        else:
            skill_score = 0.5  # No skills listed — neutral

        # Experience fit
        jd_exp = self._parse_experience_requirement(job.get("experience_required", ""))
        if jd_exp is not None:
            gap = self.candidate_years - jd_exp
            if gap >= 0:
                experience_score = 1.0
            elif gap >= -1:
                experience_score = 0.75
            elif gap >= -2:
                experience_score = 0.45
            else:
                experience_score = 0.10
        else:
            experience_score = 0.7  # Unknown requirement — slight benefit of doubt

        # Location fit
        job_location = (job.get("location") or "").lower()
        job_remote = (job.get("remote") or "").lower()

        if "remote" in job_remote or "remote" in job_location:
            location_score = 1.0
        elif any(loc in job_location for loc in self.candidate_locations):
            location_score = 1.0
        elif "hybrid" in job_remote:
            if any(loc in job_location for loc in self.candidate_locations):
                location_score = 0.7
            else:
                location_score = 0.3
        elif job_location:
            location_score = 0.0
        else:
            location_score = 0.5  # Unknown location

        # Domain fit — check JD text and company for domain signals
        domain_score = self._compute_domain_fit(job)

        # Weighted total
        total = 100 * (
            self.weights.get("skills", 0.50) * skill_score +
            self.weights.get("experience", 0.20) * experience_score +
            self.weights.get("location", 0.15) * location_score +
            self.weights.get("domain", 0.15) * domain_score
        )

        # Mandatory skill cap
        if len(missing) >= 2 and len(jd_skills_set) > 0:
            missing_ratio = len(missing) / len(jd_skills_set)
            if missing_ratio > 0.5:
                total = min(total, self.mandatory_cap)

        # Role priority boost/penalty
        role_tier = self._get_role_tier(job.get("title", ""))
        if role_tier == 1:
            total = min(total * 1.10, 100)   # +10% boost for tier 1
        elif role_tier == 2:
            pass                              # No change for tier 2
        elif role_tier == 3:
            total = total * 0.90              # -10% for tier 3

        total = round(min(total, 100), 1)

        return {
            "total": total,
            "skill_score": round(skill_score * 100, 1),
            "experience_score": round(experience_score * 100, 1),
            "location_score": round(location_score * 100, 1),
            "domain_score": round(domain_score * 100, 1),
            "matched_skills": list(matched),
            "missing_skills": list(missing),
            "role_tier": role_tier,
        }

    async def _llm_enhance(self, job: dict, scores: dict) -> dict:
        """Use LLM + RAG to add qualitative analysis to the score."""
        # Retrieve relevant resume chunks for this JD
        jd_text = job.get("full_description") or job.get("jd_summary") or ""
        relevant_chunks = self.vectorstore.query(jd_text, top_k=3)
        context = "\n".join(f"- {c['text']}" for c in relevant_chunks)

        prompt = f"""Candidate skills: {', '.join(list(self.candidate_skills)[:30])}
Candidate experience: {self.candidate_years} years
Relevant experience from resume:
{context}

Job: {job.get('title', '')} at {job.get('company', '')}
Location: {job.get('location', '')} ({job.get('remote', '')})

Job description (excerpt):
{jd_text[:2000]}

Deterministic score: {scores['total']}%
Matched skills: {', '.join(scores['matched_skills'][:10])}
Missing skills: {', '.join(scores['missing_skills'][:10])}

Analyze this match."""

        try:
            analysis = await self.llm.complete_json(
                prompt=prompt,
                system=MATCH_SYSTEM_PROMPT,
                agent=self.name,
            )
            return analysis
        except Exception as e:
            logger.warning("matching_llm_failed", error=str(e))
            return {
                "match_summary": f"Score: {scores['total']}%. Matched {len(scores['matched_skills'])} skills, missing {len(scores['missing_skills'])}.",
                "role_fit": "unknown",
            }

    @staticmethod
    def _parse_experience_requirement(exp_str: str) -> Optional[int]:
        """Extract minimum years from experience requirement string."""
        if not exp_str:
            return None
        import re
        # Try patterns like "3-5 years", "5+ years", "minimum 3 years"
        match = re.search(r'(\d+)\s*[-+]?\s*(?:to\s*\d+\s*)?(?:years|yrs|yr)', exp_str, re.IGNORECASE)
        if match:
            return int(match.group(1))
        # Try bare numbers
        match = re.search(r'(\d+)', exp_str)
        if match:
            return int(match.group(1))
        return None

    def _compute_domain_fit(self, job: dict) -> float:
        """Score how well a job's domain matches the candidate's background.

        Checks JD text, company name, and title for domain signals.
        Returns: 0.0 (weak fit) to 1.0 (strong fit).
        """
        # Build a text blob to scan for domain signals
        text = " ".join([
            (job.get("title") or ""),
            (job.get("company") or ""),
            (job.get("full_description") or ""),
            (job.get("jd_summary") or ""),
        ]).lower()

        # Check strong fit domains
        strong_hits = sum(1 for d in self.strong_domains if d in text)
        weak_hits = sum(1 for d in self.weak_domains if d in text)
        moderate_hits = sum(1 for d in self.moderate_domains if d in text)

        if strong_hits > 0 and weak_hits == 0:
            return 1.0      # Clear strong domain match
        elif strong_hits > 0 and weak_hits > 0:
            return 0.7      # Mixed signals — some overlap
        elif moderate_hits > 0 and weak_hits == 0:
            return 0.6      # Transferable domain
        elif weak_hits > 0 and strong_hits == 0:
            return 0.2      # Weak domain fit (e.g., pure B2B SaaS)
        else:
            return 0.5      # Unknown domain — neutral

    def _get_role_tier(self, title: str) -> int:
        """Determine which priority tier a job title falls into.

        Returns: 1 (best fit), 2 (good fit), 3 (open to), 0 (unknown)
        """
        title_lower = title.lower().strip()
        if any(role in title_lower for role in self.tier1_roles):
            return 1
        if any(role in title_lower for role in self.tier2_roles):
            return 2
        if any(role in title_lower for role in self.tier3_roles):
            return 3
        return 0
