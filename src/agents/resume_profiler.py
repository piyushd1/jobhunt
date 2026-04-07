"""Resume Profiler Agent — parses resume PDF into structured candidate profile.

Runs once (or when resume changes). Extracts skills, experience, projects,
and target roles via LLM, then caches the result as JSON.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pdfplumber
import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.llm import LLMClient

logger = structlog.get_logger()

SYSTEM_PROMPT = """You are a resume parser. Given the full text of a resume, extract a structured candidate profile.

Return ONLY valid JSON with this exact schema:
{
  "name": "Full Name",
  "email": "email@example.com",
  "phone": "phone number or empty string",
  "location": "City, Country",
  "target_roles": ["Product Manager", "Senior PM", ...],
  "total_experience_years": 8,
  "current_title": "Current Job Title",
  "current_company": "Current Company",
  "summary": "2-3 sentence professional summary",
  "skills": {
    "core": ["skill1", "skill2"],
    "tools": ["tool1", "tool2"],
    "methodologies": ["method1", "method2"],
    "domains": ["domain1", "domain2"]
  },
  "skill_years": {"skill_name": 3, "another_skill": 5},
  "experience": [
    {
      "company": "Company Name",
      "title": "Job Title",
      "duration": "2 years",
      "highlights": ["Led X resulting in Y", "Built Z"]
    }
  ],
  "projects": [
    {
      "name": "Project Name",
      "description": "What it was and what you did",
      "skills_used": ["skill1", "skill2"],
      "impact": "Measurable outcome"
    }
  ],
  "education": [
    {
      "degree": "MBA / B.Tech / etc",
      "institution": "University Name",
      "year": "2020"
    }
  ],
  "certifications": ["Cert 1", "Cert 2"],
  "preferred_locations": ["Bangalore", "Remote"]
}

Be thorough. Extract every skill, tool, and methodology mentioned.
For skill_years, estimate based on work history durations.
For target_roles, infer from experience and title progression."""


class ResumeProfiler(BaseAgent):
    """Parse resume PDF into a structured candidate profile."""

    name = "resume_profiler"

    def __init__(self, config: dict, llm: LLMClient):
        super().__init__(config)
        self.llm = llm
        self.resume_path = Path(config.get("resume", {}).get("path", "./data/resume.pdf"))
        self.cache_path = Path(config.get("resume", {}).get("profile_cache", "./data/candidate_profile.json"))

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

        # Add metadata and cache
        profile["_resume_hash"] = resume_hash
        profile["_source_file"] = str(self.resume_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(profile, indent=2))

        logger.info("resume_profile_created",
                     skills=len(profile.get("skills", {}).get("core", [])),
                     experience_years=profile.get("total_experience_years"),
                     projects=len(profile.get("projects", [])))

        return AgentResult(data=profile, count=1)

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
