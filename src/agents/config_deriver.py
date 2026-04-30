"""ConfigDeriver — LLM auto-generates candidate-specific config from a parsed resume.

Input: parsed candidate profile (from ResumeProfiler) + a short dict of user inputs
       (target keywords, locations, experience range).

Output: a dict that `cmd_init` writes to `config.local.yaml`. Contains:
  - search.keywords
  - search.locations
  - search.remote_ok
  - search.experience_min / experience_max / experience_buffer
  - search.role_priority (tier1 / tier2 / tier3)
  - search.excluded_title_keywords
  - matching.resume_signals
  - matching.disqualifiers
  - matching.domain_preferences (strong_fit / moderate_fit / weak_fit)
  - matching.shortlist_threshold / sheet_min_score
  - big_brand_companies
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.llm import LLMClient

logger = structlog.get_logger()


SYSTEM_PROMPT = """You generate candidate-specific job-hunt search configuration.

Given a parsed resume and the candidate's stated job-hunt preferences, produce a
strict-JSON document that downstream code uses to filter and score job listings.

Be specific and grounded in the resume. Do NOT invent skills or domains the
candidate doesn't actually have. Match disqualifiers to the candidate's
seniority (early-career → block "10+ years"; senior → block "fresher / intern").

Return ONLY valid JSON with this exact shape:
{
  "resume_signals": [
    "phrase 1", "phrase 2", ...
  ],
  "disqualifiers": [
    "phrase 1", "phrase 2", ...
  ],
  "domain_preferences": {
    "strong_fit":   ["domain1", "domain2", ...],
    "moderate_fit": ["domain1", "domain2", ...],
    "weak_fit":     ["domain1", "domain2", ...]
  },
  "excluded_title_keywords": ["keyword1", "keyword2", ...],
  "role_priority": {
    "tier1": ["title1", "title2"],
    "tier2": ["title1", "title2"],
    "tier3": ["title1", "title2"]
  },
  "big_brand_companies": ["company1", "company2", ...]
}

Rules:
- resume_signals: 30-50 lowercase phrases lifted from the candidate's actual
  work (skills, methodologies, domain words seen in their experience/projects).
  When found in a JD, they indicate the role matches their real background.
- disqualifiers: 20-30 lowercase phrases that suggest expertise the candidate
  DOES NOT have. Tune to seniority:
    * early-career (≤3 yrs): "5+ years", "8+ years", "10+ years",
      "manage product managers", "head of product", deep-domain words
      they lack
    * mid-career (4-8 yrs): seniority caps less aggressive; focus on
      domain depth they lack (e.g., core banking, embedded systems,
      hardware, compliance)
    * senior (9+ yrs): "fresher", "intern", "trainee", "0-1 years",
      junior-only signals
- domain_preferences:
    * strong_fit: 8-15 domains the candidate has clearly worked in
    * moderate_fit: 5-10 adjacent domains where transfer is plausible
    * weak_fit: 8-15 domains they have NO experience in (will be penalized)
  Use generic domain words (e.g., "marketplace", "fintech", "b2b saas").
- excluded_title_keywords: 10-20 lowercase title fragments to filter out
  (titles too senior or too junior for them, plus wrong-family roles).
- role_priority: split the candidate's TARGET keywords into 3 tiers based
  on fit. tier1 = best match for level + skills; tier3 = stretch.
- big_brand_companies: 30-80 lowercase company-name fragments where
  Project/Program Manager titles should still be considered (substring
  match on company name). Pick well-known tech companies likely to have
  high-quality PgM/TPM roles, plus any companies the candidate
  specifically mentioned wanting to work at.

Output STRICT JSON. No markdown, no commentary."""


class ConfigDeriver(BaseAgent):
    """Auto-derive candidate-specific config blocks from a parsed resume."""

    name = "config_deriver"

    def __init__(self, config: dict, llm: LLMClient):
        super().__init__(config)
        self.llm = llm

    async def derive(self, profile: dict, user_inputs: dict) -> dict:
        """Generate the candidate-specific config blocks.

        Args:
            profile: parsed candidate profile (from ResumeProfiler)
            user_inputs: dict with keys:
                target_keywords:   list[str]
                locations:         list[str]
                remote_ok:         bool
                experience_min:    int
                experience_max:    int

        Returns:
            dict with keys: resume_signals, disqualifiers, domain_preferences,
                            excluded_title_keywords, role_priority,
                            big_brand_companies
            (Empty / safe defaults on LLM failure — never raises.)
        """
        prompt = self._build_prompt(profile, user_inputs)

        try:
            response = await self.llm.complete_json(
                prompt=prompt,
                system=SYSTEM_PROMPT,
                agent=self.name,
            )
        except Exception as exc:
            logger.error("config_deriver_failed", error=str(exc))
            return self._fallback(user_inputs)

        return self._validate_and_normalize(response, user_inputs)

    async def run(self, input_data: Any = None) -> AgentResult:
        """BaseAgent contract — wraps `derive` and returns AgentResult.

        Expects input_data to be a dict {profile, user_inputs}.
        """
        if not isinstance(input_data, dict):
            return AgentResult(errors=["input_data must be {profile, user_inputs}"])
        profile = input_data.get("profile") or {}
        user_inputs = input_data.get("user_inputs") or {}
        derived = await self.derive(profile, user_inputs)
        return AgentResult(data=derived, count=1)

    # ── Internals ──────────────────────────────────────────────────

    def _build_prompt(self, profile: dict, user_inputs: dict) -> str:
        """Compose a focused prompt from the profile + user inputs."""
        years = profile.get("total_experience_years") or 0
        if years <= 3:
            seniority = "early-career"
        elif years <= 8:
            seniority = "mid-career"
        else:
            seniority = "senior"

        # Compact summary so we don't blow the context.
        skills_core = (profile.get("skills") or {}).get("core", [])[:25]
        skills_tools = (profile.get("skills") or {}).get("tools", [])[:15]
        skills_domains = (profile.get("skills") or {}).get("domains", [])
        experiences = profile.get("experience") or []
        projects = profile.get("projects") or []

        exp_blob = "\n".join(
            f"  - {e.get('title','')} at {e.get('company','')} "
            f"({e.get('duration','')}): {' / '.join(e.get('highlights',[])[:3])}"
            for e in experiences[:6]
        )
        proj_blob = "\n".join(
            f"  - {p.get('name','')}: {p.get('description','')[:200]}"
            for p in projects[:5]
        )

        target_keywords = ", ".join(user_inputs.get("target_keywords") or [])
        target_locations = ", ".join(user_inputs.get("locations") or [])
        remote = "yes" if user_inputs.get("remote_ok") else "no"
        emin = user_inputs.get("experience_min")
        emax = user_inputs.get("experience_max")

        return f"""CANDIDATE PROFILE
Name: {profile.get('name','(unknown)')}
Current title: {profile.get('current_title','')}
Current company: {profile.get('current_company','')}
Years of experience: {years}  (level: {seniority})
Summary: {profile.get('summary','')}

Core skills: {', '.join(skills_core)}
Tools: {', '.join(skills_tools)}
Domain experience: {', '.join(skills_domains)}

Recent experience (most recent first):
{exp_blob or '  (none)'}

Notable projects:
{proj_blob or '  (none)'}

JOB-HUNT PREFERENCES (user-supplied)
Target role keywords: {target_keywords}
Target locations:     {target_locations}
Open to remote:       {remote}
Experience range:     {emin} – {emax} years (HARD filter)

INSTRUCTIONS
Return strict JSON matching the schema in the system message. Tune disqualifiers
and excluded_title_keywords to a {seniority} candidate.
"""

    def _validate_and_normalize(self, response: dict, user_inputs: dict) -> dict:
        """Validate the LLM response shape and apply minimal normalization.

        Missing fields fall back to empty lists / safe defaults rather than
        raising — the wizard prefers a partially-derived config over a crash.
        """
        out = {
            "resume_signals": [],
            "disqualifiers": [],
            "domain_preferences": {
                "strong_fit": [],
                "moderate_fit": [],
                "weak_fit": [],
            },
            "excluded_title_keywords": [],
            "role_priority": {"tier1": [], "tier2": [], "tier3": []},
            "big_brand_companies": [],
        }

        if not isinstance(response, dict):
            logger.warning("config_deriver_response_not_dict",
                           response_type=type(response).__name__)
            return self._fallback(user_inputs, base=out)

        # resume_signals / disqualifiers / excluded_title_keywords / big_brand_companies
        for k in ("resume_signals", "disqualifiers",
                  "excluded_title_keywords", "big_brand_companies"):
            v = response.get(k)
            if isinstance(v, list):
                out[k] = [str(x).strip().lower() for x in v if isinstance(x, str) and x.strip()]

        # domain_preferences
        dp = response.get("domain_preferences")
        if isinstance(dp, dict):
            for bucket in ("strong_fit", "moderate_fit", "weak_fit"):
                v = dp.get(bucket)
                if isinstance(v, list):
                    out["domain_preferences"][bucket] = [
                        str(x).strip().lower() for x in v if isinstance(x, str) and x.strip()
                    ]

        # role_priority
        rp = response.get("role_priority")
        if isinstance(rp, dict):
            for tier in ("tier1", "tier2", "tier3"):
                v = rp.get(tier)
                if isinstance(v, list):
                    out["role_priority"][tier] = [
                        str(x).strip().lower() for x in v if isinstance(x, str) and x.strip()
                    ]

        # If role_priority.tier1 came back empty, default to user's target keywords.
        if not out["role_priority"]["tier1"]:
            out["role_priority"]["tier1"] = [
                k.lower() for k in (user_inputs.get("target_keywords") or [])
            ]

        return out

    def _fallback(self, user_inputs: dict, base: dict = None) -> dict:
        """Best-effort fallback when the LLM call fails."""
        out = base or {
            "resume_signals": [],
            "disqualifiers": [],
            "domain_preferences": {"strong_fit": [], "moderate_fit": [], "weak_fit": []},
            "excluded_title_keywords": [],
            "role_priority": {"tier1": [], "tier2": [], "tier3": []},
            "big_brand_companies": [],
        }
        # At minimum, populate role_priority.tier1 from target keywords.
        out["role_priority"]["tier1"] = [
            k.lower() for k in (user_inputs.get("target_keywords") or [])
        ]
        # Generic age-based exclusions
        emin = user_inputs.get("experience_min") or 0
        if emin <= 3:
            out["excluded_title_keywords"] = [
                "senior", "lead", "principal", "staff", "director", "vp",
                "head of", "chief", "intern", "fresher", "trainee",
            ]
        else:
            out["excluded_title_keywords"] = ["intern", "fresher", "trainee"]
        return out
