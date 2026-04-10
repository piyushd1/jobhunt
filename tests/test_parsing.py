import pytest

from src.agents.parsing import ParsingAgent
from src.core.roles import ROLE_FAMILY_PM_CORE


class FakeLLM:
    async def complete_json(self, *args, **kwargs):
        return {
            "title": "Senior Product Manager",
            "company": "Acme",
            "location": "Bangalore",
            "remote": "Hybrid",
            "experience_required": "8-12 years",
            "skills_required": ["SQL", "Roadmapping", "SQL"],
            "skills_preferred": ["Figma"],
            "summary": "Owns consumer marketplace growth.",
        }


class FakePage:
    pass


@pytest.mark.asyncio
async def test_parse_job_keeps_required_and_preferred_skills_separate(monkeypatch):
    parser = ParsingAgent(config={}, db=object(), browser_ctx=object(), llm=FakeLLM())

    async def _safe_goto(*args, **kwargs):
        return True

    async def _no_delay(*args, **kwargs):
        return None

    async def _extract_jd(*args, **kwargs):
        return "This is a long marketplace PM description focused on strategy and execution."

    async def _extract_apply(*args, **kwargs):
        return "https://boards.greenhouse.io/acme/jobs/1"

    monkeypatch.setattr("src.agents.parsing.safe_goto", _safe_goto)
    monkeypatch.setattr("src.agents.parsing.human_delay", _no_delay)
    monkeypatch.setattr(parser, "_extract_jd_text", _extract_jd)
    monkeypatch.setattr(parser, "_extract_apply_link", _extract_apply)

    updates = await parser._parse_job(
        FakePage(),
        {
            "url": "https://www.linkedin.com/jobs/view/123",
            "title": "View details",
            "company": "",
            "location": "job",
        },
    )

    assert updates["required_skills"] == ["SQL", "Roadmapping"]
    assert updates["preferred_skills"] == ["Figma"]
    assert updates["skills_required"] == ["SQL", "Roadmapping", "Figma"]
    assert updates["title"] == "Senior Product Manager"
    assert updates["company"] == "Acme"
    assert updates["location"] == "Bangalore"
    assert updates["role_family_hint"] == ROLE_FAMILY_PM_CORE
