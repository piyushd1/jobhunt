"""Resume Profiler Agent — parses resume PDF into structured candidate profile.

Runs once (or when resume changes). Extracts skills, experience, projects,
and target roles via LLM, then canonicalizes skills against the PM taxonomy
with embedding-based fuzzy matching as fallback.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

import pdfplumber
import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.embeddings import EmbeddingModel
from src.core.llm import LLMClient
from src.core.skills import SKILL_TAXONOMY, SkillCanonicalizer

logger = structlog.get_logger()

# Build a flat skill list for the LLM prompt so it knows what to look for
_SKILL_REFERENCE = []
for _cat, _skills in SKILL_TAXONOMY.items():
    _SKILL_REFERENCE.extend(_skills.keys())
_SKILL_LIST_STR = ", ".join(_SKILL_REFERENCE)

SYSTEM_PROMPT = f"""You are an expert resume parser specializing in Product Management, Project Management, Program Management, and Product Owner roles.

Given the full text of a resume, extract a structured candidate profile.

IMPORTANT — for skills extraction, be exhaustive. Look for:
- Explicit skills listed in a skills section
- Implicit skills demonstrated in job descriptions and projects
- Tools and platforms mentioned anywhere
- Methodologies and frameworks used
- Domain expertise inferred from industry experience

Here is a reference list of PM-relevant skills. Match the candidate's skills to these when possible, but also extract any additional skills not on this list:
{_SKILL_LIST_STR}

Return ONLY valid JSON with this exact schema:
{{
  "name": "Full Name",
  "email": "email@example.com",
  "phone": "phone number or empty string",
  "location": "City, Country",
  "target_roles": ["Product Manager", "Senior PM", "Project Manager", "Program Manager", "Product Owner"],
  "total_experience_years": 8,
  "current_title": "Current Job Title",
  "current_company": "Current Company",
  "summary": "2-3 sentence professional summary highlighting PM-relevant strengths",
  "skills": {{
    "core": ["Product Strategy", "Roadmapping", "Stakeholder Management", ...],
    "tools": ["Jira", "Figma", "SQL", ...],
    "methodologies": ["Agile/Scrum", "Design Thinking", "A/B Testing", ...],
    "domains": ["B2B SaaS", "Fintech", ...]
  }},
  "skill_years": {{"Product Strategy": 5, "Agile/Scrum": 8, ...}},
  "experience": [
    {{
      "company": "Company Name",
      "title": "Job Title",
      "duration": "2 years",
      "highlights": ["Led X resulting in Y", "Built Z"],
      "skills_demonstrated": ["Roadmapping", "Cross-functional Leadership"]
    }}
  ],
  "projects": [
    {{
      "name": "Project Name",
      "description": "What it was and what you did",
      "skills_used": ["skill1", "skill2"],
      "impact": "Measurable outcome"
    }}
  ],
  "education": [
    {{
      "degree": "MBA / B.Tech / etc",
      "institution": "University Name",
      "year": "2020"
    }}
  ],
  "certifications": ["Cert 1", "Cert 2"],
  "preferred_locations": ["Bangalore", "Remote"]
}}

Be thorough. Extract EVERY skill, tool, and methodology mentioned or implied.
For skill_years, estimate based on work history durations where each skill was used.
For target_roles, infer from experience and title progression.
For skills_demonstrated in experience, list the PM skills evident from each role."""


class ResumeProfiler(BaseAgent):
    """Parse resume PDF into a structured candidate profile with canonicalized skills."""

    name = "resume_profiler"

    def __init__(self, config: dict, llm: LLMClient, embedding_model: Optional[EmbeddingModel] = None):
        super().__init__(config)
        self.llm = llm
        self.resume_path = Path(config.get("resume", {}).get("path", "./data/resume.pdf"))
        self.cache_path = Path(config.get("resume", {}).get("profile_cache", "./data/candidate_profile.json"))
        self.canonicalizer = SkillCanonicalizer(
            embedding_model=embedding_model,
            similarity_threshold=0.55,
        )

    async def run(self, input_data: Any = None) -> AgentResult:
        """Parse resume and return structured profile. Uses cache if resume unchanged."""
        if not self.resume_path.exists():
            return AgentResult(errors=[f"Resume not found: {self.resume_path}"])

        # Check if cached profile is still valid
        resume_hash = self._file_hash(self.resume_path)
        if self.cache_path.exists():
            cached = json.loads(self.cache_path.read_text())
            if cached.get("_resume_hash") == resume_hash:
                logger.info("resume_profile_cached", hash=resume_hash[:8])
                return AgentResult(data=cached, count=1)

        # Extract text from PDF
        resume_text = self._extract_text()
        if not resume_text.strip():
            return AgentResult(errors=["Could not extract text from resume PDF"])

        logger.info("resume_text_extracted", chars=len(resume_text))

        # Send to LLM for structured extraction
        try:
            profile = await self.llm.complete_json(
                prompt=f"Parse this resume:\n\n{resume_text}",
                system=SYSTEM_PROMPT,
                agent=self.name,
            )
        except Exception as e:
            return AgentResult(errors=[f"LLM parsing failed: {e}"])

        # Canonicalize all skills against taxonomy
        profile = self._canonicalize_profile_skills(profile)

        # Add metadata and cache
        profile["_resume_hash"] = resume_hash
        profile["_source_file"] = str(self.resume_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(profile, indent=2))

        # Log summary
        all_skills = self._get_all_skills(profile)
        canon_results = profile.get("_skill_canonicalization", {})
        logger.info("resume_profile_created",
                     total_skills=len(all_skills),
                     exact_matches=canon_results.get("exact", 0),
                     embedding_matches=canon_results.get("embedding", 0),
                     unmatched=canon_results.get("unmatched", 0),
                     experience_years=profile.get("total_experience_years"),
                     projects=len(profile.get("projects", [])))

        return AgentResult(data=profile, count=1)

    def _canonicalize_profile_skills(self, profile: dict) -> dict:
        """Canonicalize all skills in the profile against the PM taxonomy."""
        # Gather all raw skills from every section
        all_raw_skills = set()
        skills_section = profile.get("skills", {})
        for category in ["core", "tools", "methodologies", "domains"]:
            all_raw_skills.update(skills_section.get(category, []))

        # Also from experience and projects
        for exp in profile.get("experience", []):
            all_raw_skills.update(exp.get("skills_demonstrated", []))
        for proj in profile.get("projects", []):
            all_raw_skills.update(proj.get("skills_used", []))

        # Canonicalize
        results = self.canonicalizer.canonicalize_many(list(all_raw_skills))

        # Rebuild skills section using canonical names, organized by taxonomy category
        canonical_by_category: dict[str, list[str]] = {
            "product_strategy": [],
            "product_execution": [],
            "technical_skills": [],
            "analytics_tools": [],
            "product_tools": [],
            "ux_research": [],
            "leadership": [],
            "domain_knowledge": [],
            "program_project_management": [],
            "other": [],
        }
        for r in results:
            canonical_by_category.setdefault(r["category"], []).append(r["canonical"])

        profile["skills_canonical"] = {k: v for k, v in canonical_by_category.items() if v}

        # Also keep a flat list for easy matching
        profile["all_skills_canonical"] = [r["canonical"] for r in results]

        # Canonicalize skill_years keys
        old_skill_years = profile.get("skill_years", {})
        new_skill_years = {}
        for raw_skill, years in old_skill_years.items():
            canonical, _ = self.canonicalizer.canonicalize(raw_skill)
            new_skill_years[canonical] = max(new_skill_years.get(canonical, 0), years)
        profile["skill_years"] = new_skill_years

        # Track canonicalization stats
        method_counts = {"exact": 0, "embedding": 0, "unmatched": 0}
        for r in results:
            method_counts[r["method"]] += 1
        profile["_skill_canonicalization"] = method_counts

        return profile

    @staticmethod
    def _get_all_skills(profile: dict) -> list[str]:
        """Get all canonical skills from a profile."""
        return profile.get("all_skills_canonical", [])

    def _extract_text(self) -> str:
        """Extract all text from the resume PDF."""
        text_parts = []
        with pdfplumber.open(self.resume_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n\n".join(text_parts)

    @staticmethod
    def _file_hash(path: Path) -> str:
        """MD5 hash of a file for change detection."""
        return hashlib.md5(path.read_bytes()).hexdigest()
