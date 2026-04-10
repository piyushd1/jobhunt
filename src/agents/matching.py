"""Matching Agent — precision-first scoring for PM, TPM, and PgM roles."""

import json
from typing import Any, Optional

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.db import Database
from src.core.llm import LLMClient
from src.core.roles import (
    DEFAULT_ALLOWED_ROLE_FAMILIES,
    ROLE_FAMILY_PM_CORE,
    ROLE_FAMILY_TPM_PGM,
    classify_role_family,
)
from src.core.skills import SkillCanonicalizer
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

ROLE_FIT_SCORES = {
    ROLE_FAMILY_PM_CORE: 1.0,
    ROLE_FAMILY_TPM_PGM: 0.92,
}

DISQUALIFIER_CLUSTERS = {
    "b2b_saas": [
        "enterprise sales experience", "saas sales", "b2b saas experience",
        "enterprise customer", "large account", "crm platform", "salesforce",
    ],
    "regulated_finance": [
        "core banking", "banking domain", "lending platform", "loan",
        "underwriting", "insurance product", "claims management",
        "wealth management", "asset management",
    ],
    "compliance": [
        "regulatory compliance", "soc 2", "hipaa", "pci dss",
    ],
    "developer_platform": [
        "developer experience", "sdk", "developer tools", "devtools",
    ],
    "hardware_industrial": [
        "embedded system", "firmware", "semiconductor", "chip design",
        "telecom", "networking protocol", "hardware product", "manufacturing",
        "supply chain management", "procurement", "erp implementation", "iot",
    ],
}

CLUSTER_LABELS = {
    "b2b_saas": "pure B2B SaaS/enterprise-sales expectations",
    "regulated_finance": "core banking or insurance-style domain depth",
    "compliance": "heavy compliance or regulated-domain requirements",
    "developer_platform": "developer tooling/platform specialization",
    "hardware_industrial": "hardware, telecom, or industrial-ops specialization",
}


class MatchingAgent(BaseAgent):
    """Score and rank jobs against the candidate profile."""

    name = "matching"

    def __init__(
        self,
        config: dict,
        db: Database,
        llm: LLMClient,
        vectorstore: ResumeVectorStore,
        canonicalizer: SkillCanonicalizer,
        profile: dict,
    ):
        super().__init__(config)
        self.db = db
        self.llm = llm
        self.vectorstore = vectorstore
        self.canonicalizer = canonicalizer
        self.profile = profile

        match_config = config.get("matching", {})
        configured_weights = match_config.get("weights", {})
        self.weights = {
            "required_skills": configured_weights.get("required_skills", 0.25),
            "preferred_skills": configured_weights.get("preferred_skills", 0.10),
            "experience": configured_weights.get("experience", 0.15),
            "location": configured_weights.get("location", 0.10),
            "domain": configured_weights.get("domain", 0.25),
            "role_fit": configured_weights.get("role_fit", 0.15),
        }
        self.mandatory_cap = match_config.get("mandatory_skill_cap", 55)
        self.allowed_role_families = set(
            config.get("search", {}).get("allowed_role_families", DEFAULT_ALLOWED_ROLE_FAMILIES)
        )

        # Resume signals — phrases from YOUR work that indicate good JD fit
        self.resume_signals = [s.lower() for s in match_config.get("resume_signals", [])]
        # Domain keyword groups (broader company-level signals)
        domain_prefs = match_config.get("domain_preferences", {})
        self.strong_domains = [d.lower() for d in domain_prefs.get("strong_fit", [])]
        self.moderate_domains = [d.lower() for d in domain_prefs.get("moderate_fit", [])]
        self.weak_domains = [d.lower() for d in domain_prefs.get("weak_fit", [])]

        # Candidate data
        self.candidate_skills = set(profile.get("all_skills_canonical", []))
        self.candidate_years = profile.get("total_experience_years", 0)

        profile_locations = [loc.lower() for loc in profile.get("preferred_locations", []) if loc]
        if not profile_locations:
            search = config.get("search", {})
            fallback_locations = search.get("locations", [])
            if not fallback_locations:
                single_location = search.get("location")
                fallback_locations = [single_location] if single_location else []
            profile_locations = [loc.lower() for loc in fallback_locations if loc]
        self.candidate_locations = set(profile_locations)

        self.candidate_domains = profile.get("skills", {}).get("domains", [])
        self.candidate_summary = profile.get("summary", "")
        self.candidate_companies = self._extract_candidate_companies(profile)

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
                role_family = self._resolve_role_family(job)
                if role_family not in self.allowed_role_families:
                    self._mark_out_of_scope(job, role_family)
                    scored_count += 1
                    continue

                scores = self._compute_deterministic_score(job, role_family=role_family)
                llm_analysis = await self._llm_enhance(job, scores)

                self.db.update_job(
                    job["id"],
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
                scored_count += 1

                logger.info(
                    "matching_job_scored",
                    progress=f"{i+1}/{len(unscored)}",
                    title=job.get("title", "")[:40],
                    score=scores["total"],
                    fit_bucket=scores["fit_bucket"],
                    role_family=role_family,
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

    def _compute_deterministic_score(self, job: dict, role_family: str) -> dict:
        """Compute the weighted precision-first score."""
        required_raw = self._load_skill_list(job, "required_skills")
        preferred_raw = self._load_skill_list(job, "preferred_skills")
        if not required_raw and not preferred_raw:
            required_raw = self._load_skill_list(job, "skills_required")

        required_set = self._canonicalize_skills(required_raw)
        preferred_set = self._canonicalize_skills(preferred_raw) - required_set

        matched_required = self.candidate_skills & required_set
        missing_required = required_set - self.candidate_skills
        matched_preferred = self.candidate_skills & preferred_set
        missing_preferred = preferred_set - self.candidate_skills

        required_skill_score = len(matched_required) / len(required_set) if required_set else 0.5
        preferred_skill_score = len(matched_preferred) / len(preferred_set) if preferred_set else 0.5

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
            experience_score = 0.65

        # Location fit
        job_location = (job.get("location") or "").lower()
        job_remote = (job.get("remote") or "").lower()
        if "remote" in job_remote or "remote" in job_location:
            location_score = 1.0
        elif any(loc in job_location for loc in self.candidate_locations):
            location_score = 1.0
        elif "hybrid" in job_remote:
            location_score = 0.7 if self.candidate_locations else 0.5
        elif job_location:
            location_score = 0.2
        else:
            location_score = 0.5

        domain_analysis = self._compute_domain_fit(job)
        domain_score = domain_analysis["score"]

        role_fit_score = ROLE_FIT_SCORES.get(role_family, 0.0)

        total = 100 * (
            self.weights["required_skills"] * required_skill_score +
            self.weights["preferred_skills"] * preferred_skill_score +
            self.weights["experience"] * experience_score +
            self.weights["location"] * location_score +
            self.weights["domain"] * domain_score +
            self.weights["role_fit"] * role_fit_score
        )
        if domain_analysis.get("cap_total") is not None:
            total = min(total, domain_analysis["cap_total"])

        penalty_reasons = list(domain_analysis["reasons"])
        notable_mismatch_count = domain_analysis["notable_count"]
        severe_mismatch = domain_analysis["severe_mismatch"]

        missing_skills = sorted(missing_required)
        for skill in sorted(missing_preferred):
            if skill not in missing_skills:
                missing_skills.append(skill)

        if required_set:
            missing_required_ratio = len(missing_required) / len(required_set)
        else:
            missing_required_ratio = 0.0

        if missing_required:
            sample = ", ".join(sorted(missing_required)[:4])
            if missing_required_ratio > 0.5:
                severe_mismatch = True
                penalty_reasons.append(f"Missing most required skills: {sample}")
            else:
                notable_mismatch_count += 1
                penalty_reasons.append(f"Missing required skills: {sample}")
            total = min(total, self.mandatory_cap) if missing_required_ratio > 0.5 else total

        if location_score <= 0.2 and job_location:
            notable_mismatch_count += 1
            penalty_reasons.append(f"Location mismatch: {job.get('location', '')}")

        total = round(min(total, 100), 1)
        fit_bucket = self._fit_bucket(
            total=total,
            notable_mismatch_count=notable_mismatch_count,
            severe_mismatch=severe_mismatch,
        )

        matched_skills = sorted(matched_required | matched_preferred)
        fallback_summary = self._build_fallback_summary(
            total=total,
            role_family=role_family,
            fit_bucket=fit_bucket,
            penalty_reasons=penalty_reasons,
            matched_skills=matched_skills,
        )

        combined_skill_score = (
            (
                self.weights["required_skills"] * required_skill_score +
                self.weights["preferred_skills"] * preferred_skill_score
            ) / max(self.weights["required_skills"] + self.weights["preferred_skills"], 0.01)
        )

        return {
            "total": total,
            "skill_score": round(combined_skill_score * 100, 1),
            "required_skill_score": round(required_skill_score * 100, 1),
            "preferred_skill_score": round(preferred_skill_score * 100, 1),
            "experience_score": round(experience_score * 100, 1),
            "location_score": round(location_score * 100, 1),
            "domain_score": round(domain_score * 100, 1),
            "role_fit_score": round(role_fit_score * 100, 1),
            "matched_skills": matched_skills,
            "missing_skills": missing_skills,
            "fit_bucket": fit_bucket,
            "penalty_reasons": penalty_reasons,
            "fallback_summary": fallback_summary,
        }

    async def _llm_enhance(self, job: dict, scores: dict) -> dict:
        """Use LLM + RAG to add qualitative analysis to the score."""
        jd_text = job.get("full_description") or job.get("jd_summary") or ""
        relevant_chunks = self.vectorstore.query(jd_text, top_k=3)
        context = "\n".join(f"- {c['text']}" for c in relevant_chunks)
        company_context = ", ".join(self.candidate_companies[:3]) or "recent marketplace companies"
        penalty_text = "; ".join(scores["penalty_reasons"]) or "None"

        prompt = f"""CANDIDATE BACKGROUND:
{self.candidate_summary}
Core domains: {', '.join(self.candidate_domains)}
Experience: {self.candidate_years} years
Relevant company history: {company_context}

Relevant experience for this role:
{context}

JOB:
{job.get('title', '')} at {job.get('company', '')}
Location: {job.get('location', '')} ({job.get('remote', '')})
Role family: {job.get('role_family_hint') or self._resolve_role_family(job)}

Job description (excerpt):
{jd_text[:2000]}

SCORING:
Deterministic score: {scores['total']}%
Fit bucket: {scores['fit_bucket']}
Matched skills: {', '.join(scores['matched_skills'][:10])}
Missing skills: {', '.join(scores['missing_skills'][:10])}
Penalty reasons: {penalty_text}

IMPORTANT: Stay grounded in the candidate's actual marketplace, consumer, and AI/product strengths. Call out mismatches clearly when the JD leans toward domains the candidate does not deeply own.

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
                "match_summary": scores["fallback_summary"],
                "role_fit": scores["fit_bucket"],
            }

    def _compute_domain_fit(self, job: dict) -> dict:
        """Score how well the role maps to the candidate's actual work patterns."""
        text = " ".join([
            (job.get("title") or ""),
            (job.get("company") or ""),
            (job.get("full_description") or ""),
            (job.get("jd_summary") or ""),
            (job.get("snippet") or ""),
        ]).lower()

        signal_hits = sum(1 for s in self.resume_signals if s in text)
        strong_hits = sum(1 for d in self.strong_domains if d in text)
        moderate_hits = sum(1 for d in self.moderate_domains if d in text)
        weak_hits = sum(1 for d in self.weak_domains if d in text)
        disqualifier_clusters = self._detect_disqualifier_clusters(text)
        cluster_count = len(disqualifier_clusters)

        reasons = []
        severe_mismatch = False
        notable_count = 0
        cap_total = None

        if cluster_count >= 2 and signal_hits <= 1:
            severe_mismatch = True
            notable_count = 2
            cap_total = 55
            reasons.extend(
                f"Domain mismatch risk: {CLUSTER_LABELS.get(cluster, cluster)}"
                for cluster in disqualifier_clusters[:2]
            )
            score = 0.12
        elif cluster_count >= 2:
            severe_mismatch = True
            notable_count = 2
            cap_total = 60
            reasons.extend(
                f"Mixed domain risk: {CLUSTER_LABELS.get(cluster, cluster)}"
                for cluster in disqualifier_clusters[:2]
            )
            score = 0.28
        elif cluster_count == 1:
            notable_count = 1
            reasons.append(
                f"Domain mismatch risk: {CLUSTER_LABELS.get(disqualifier_clusters[0], disqualifier_clusters[0])}"
            )
            if signal_hits >= 3:
                score = 0.55
            elif signal_hits >= 1 or strong_hits >= 1:
                cap_total = 68
                score = 0.45
            else:
                cap_total = 58
                score = 0.30
        else:
            if signal_hits >= 4:
                score = 1.0
            elif signal_hits >= 2 and strong_hits >= 1:
                score = 0.95
            elif signal_hits >= 2:
                score = 0.88
            elif signal_hits >= 1 and strong_hits >= 1:
                score = 0.82
            elif strong_hits >= 1:
                score = 0.75
            elif moderate_hits >= 1:
                score = 0.62
            elif weak_hits >= 1:
                notable_count = 1
                cap_total = 65
                reasons.append("Weak domain alignment relative to core marketplace strengths")
                score = 0.40
            else:
                score = 0.50

        return {
            "score": score,
            "reasons": reasons,
            "notable_count": notable_count,
            "severe_mismatch": severe_mismatch,
            "cap_total": cap_total,
        }

    def _resolve_role_family(self, job: dict) -> str:
        hinted = job.get("role_family_hint")
        if hinted:
            return hinted
        return classify_role_family(
            job.get("title", ""),
            job.get("jd_summary", "") or job.get("full_description", ""),
        )

    def _mark_out_of_scope(self, job: dict, role_family: str) -> None:
        summary = f"Filtered as out-of-scope role family: {role_family}."
        self.db.update_job(
            job["id"],
            match_score=0.0,
            skill_score=0.0,
            required_skill_score=0.0,
            preferred_skill_score=0.0,
            experience_score=0.0,
            location_score=0.0,
            domain_score=0.0,
            role_fit_score=0.0,
            matched_skills=[],
            missing_skills=[],
            role_family=role_family,
            fit_bucket="weak_fit",
            penalty_reasons=[summary],
            match_summary=summary,
        )

    def _fit_bucket(self, total: float, notable_mismatch_count: int, severe_mismatch: bool) -> str:
        if total < 60 or severe_mismatch or notable_mismatch_count >= 2:
            return "weak_fit"
        if total >= 75 and notable_mismatch_count == 0:
            return "strong_fit"
        return "review_fit"

    @staticmethod
    def _parse_experience_requirement(exp_str: str) -> Optional[int]:
        """Extract minimum years from experience requirement string."""
        if not exp_str:
            return None
        import re
        match = re.search(r"(\d+)\s*[-+]?\s*(?:to\s*\d+\s*)?(?:years|yrs|yr)", exp_str, re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.search(r"(\d+)", exp_str)
        if match:
            return int(match.group(1))
        return None

    def _load_skill_list(self, job: dict, field: str) -> list[str]:
        raw = job.get(field)
        if not raw:
            return []
        if isinstance(raw, list):
            return raw
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _canonicalize_skills(self, raw_skills: list[str]) -> set[str]:
        skills = set()
        for raw in raw_skills:
            canonical, _ = self.canonicalizer.canonicalize(raw)
            if canonical:
                skills.add(canonical)
        return skills

    def _detect_disqualifier_clusters(self, text: str) -> list[str]:
        matches = []
        for cluster, keywords in DISQUALIFIER_CLUSTERS.items():
            if any(keyword in text for keyword in keywords):
                matches.append(cluster)
        return matches

    @staticmethod
    def _extract_candidate_companies(profile: dict) -> list[str]:
        companies = []
        current_company = profile.get("current_company")
        if current_company:
            companies.append(current_company)
        for exp in profile.get("experience", []):
            company = exp.get("company")
            if company and company not in companies:
                companies.append(company)
        return companies

    @staticmethod
    def _build_fallback_summary(
        total: float,
        role_family: str,
        fit_bucket: str,
        penalty_reasons: list[str],
        matched_skills: list[str],
    ) -> str:
        matched_preview = ", ".join(matched_skills[:4]) or "few direct skill matches"
        if penalty_reasons:
            return (
                f"{fit_bucket} at {total}%. Role family: {role_family}. "
                f"Strengths include {matched_preview}. Key risks: {'; '.join(penalty_reasons[:2])}."
            )
        return f"{fit_bucket} at {total}%. Role family: {role_family}. Strengths include {matched_preview}."
