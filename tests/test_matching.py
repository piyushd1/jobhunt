import json

from src.agents.matching import MatchingAgent
from src.core.roles import ROLE_FAMILY_PM_CORE, ROLE_FAMILY_TPM_PGM
from src.core.skills import SkillCanonicalizer


class FakeLLM:
    async def complete_json(self, *args, **kwargs):
        return {"match_summary": "ok", "role_fit": "strong_fit"}


class FakeVectorStore:
    def query(self, *args, **kwargs):
        return []


def _matcher(profile_locations=None):
    config = {
        "search": {
            "allowed_role_families": ["pm_core", "tpm_pgm"],
            "locations": ["Bangalore", "Hyderabad"],
        },
        "matching": {
            "weights": {
                "required_skills": 0.25,
                "preferred_skills": 0.10,
                "experience": 0.15,
                "location": 0.10,
                "domain": 0.25,
                "role_fit": 0.15,
            },
            "mandatory_skill_cap": 55,
            "resume_signals": [
                "marketplace",
                "consumer",
                "pricing",
                "monetization",
                "growth",
                "platform",
                "ai product",
            ],
            "domain_preferences": {
                "strong_fit": ["marketplace", "consumer", "b2c", "platform"],
                "moderate_fit": ["fintech", "payments", "ai"],
                "weak_fit": ["b2b saas", "enterprise software", "core banking", "insurance"],
            },
        },
    }
    profile = {
        "all_skills_canonical": [
            "Product Strategy",
            "Product Roadmapping",
            "SQL",
            "Stakeholder Management",
            "Program Management",
            "APIs",
            "Agile/Scrum",
        ],
        "total_experience_years": 12,
        "preferred_locations": profile_locations or [],
        "summary": "Marketplace product leader with consumer growth and AI experience.",
        "skills": {"domains": ["Marketplace", "B2C", "AI"]},
        "experience": [
            {"company": "Justdial"},
            {"company": "Urban Company"},
        ],
    }
    return MatchingAgent(
        config=config,
        db=object(),
        llm=FakeLLM(),
        vectorstore=FakeVectorStore(),
        canonicalizer=SkillCanonicalizer(),
        profile=profile,
    )


def _job(
    title,
    required,
    preferred,
    description,
    location="Bangalore",
    remote="Hybrid",
    experience="8-12 years",
    role_family_hint=None,
):
    return {
        "title": title,
        "company": "Acme",
        "location": location,
        "remote": remote,
        "experience_required": experience,
        "required_skills": json.dumps(required),
        "preferred_skills": json.dumps(preferred),
        "skills_required": json.dumps(required + preferred),
        "full_description": description,
        "jd_summary": description[:120],
        "role_family_hint": role_family_hint,
    }


def test_pm_core_role_outscores_adjacent_role_with_same_job_context():
    matcher = _matcher()
    job = _job(
        "Senior Product Manager",
        ["Product Strategy", "SQL"],
        ["Figma"],
        "Consumer marketplace product with pricing and monetization ownership.",
    )

    pm_scores = matcher._compute_deterministic_score(job, role_family=ROLE_FAMILY_PM_CORE)
    adjacent_scores = matcher._compute_deterministic_score(job, role_family="adjacent")

    assert pm_scores["total"] > adjacent_scores["total"]


def test_strong_marketplace_pm_role_scores_as_strong_fit_with_location_fallback():
    matcher = _matcher(profile_locations=[])
    job = _job(
        "Senior Product Manager",
        ["Product Strategy", "Product Roadmapping", "SQL"],
        ["Figma"],
        "Consumer marketplace role focused on pricing, monetization, growth, and platform strategy.",
    )

    scores = matcher._compute_deterministic_score(job, role_family=ROLE_FAMILY_PM_CORE)

    assert matcher.candidate_locations == {"bangalore", "hyderabad"}
    assert scores["location_score"] == 100.0
    assert scores["fit_bucket"] == "strong_fit"
    assert scores["total"] >= 75


def test_tpm_role_stays_eligible_with_slight_role_fit_penalty():
    matcher = _matcher()
    job = _job(
        "Technical Program Manager",
        ["Program Management", "Stakeholder Management", "APIs"],
        ["SQL"],
        "Platform role coordinating APIs and cross-functional delivery.",
        role_family_hint=ROLE_FAMILY_TPM_PGM,
    )

    scores = matcher._compute_deterministic_score(job, role_family=ROLE_FAMILY_TPM_PGM)

    assert scores["role_fit_score"] == 92.0
    assert scores["total"] > 60
    assert scores["fit_bucket"] in {"strong_fit", "review_fit"}


def test_consumer_fintech_role_remains_visible_without_domain_penalty():
    matcher = _matcher()
    job = _job(
        "Product Manager",
        ["Product Strategy", "SQL"],
        ["APIs"],
        "Consumer fintech payments product with marketplace-like growth loops and pricing ownership.",
    )

    scores = matcher._compute_deterministic_score(job, role_family=ROLE_FAMILY_PM_CORE)

    assert scores["domain_score"] >= 60
    assert scores["fit_bucket"] != "weak_fit"


def test_b2b_saas_role_is_soft_penalized_not_filtered_out():
    matcher = _matcher()
    job = _job(
        "Senior Product Manager",
        ["Product Strategy", "SQL"],
        ["APIs"],
        "Pure B2B SaaS role for enterprise customer workflows, large account management, and SaaS sales alignment.",
    )

    scores = matcher._compute_deterministic_score(job, role_family=ROLE_FAMILY_PM_CORE)

    assert 0 < scores["total"] < 60
    assert scores["domain_score"] < 50
    assert scores["fit_bucket"] == "weak_fit"


def test_core_banking_role_is_soft_penalized_not_filtered_out():
    matcher = _matcher()
    job = _job(
        "Product Manager",
        ["Product Strategy", "SQL"],
        ["Stakeholder Management"],
        "Core banking platform with underwriting, regulatory compliance, and insurance-style claims workflows.",
    )

    scores = matcher._compute_deterministic_score(job, role_family=ROLE_FAMILY_PM_CORE)

    assert 0 < scores["total"] < 60
    assert scores["domain_score"] < 50
    assert scores["fit_bucket"] == "weak_fit"


def test_mandatory_skill_cap_only_triggers_on_required_skill_gaps():
    matcher = _matcher()

    required_gap_job = _job(
        "Senior Product Manager",
        ["Product Strategy", "Go-to-Market Strategy", "Pricing Strategy", "Market Research"],
        ["Figma"],
        "Consumer marketplace role with strong growth and monetization signals.",
    )
    preferred_gap_job = _job(
        "Senior Product Manager",
        ["Product Strategy", "Product Roadmapping"],
        ["Figma", "Pricing Strategy"],
        "Consumer marketplace role with strong growth and monetization signals.",
    )

    required_scores = matcher._compute_deterministic_score(required_gap_job, role_family=ROLE_FAMILY_PM_CORE)
    preferred_scores = matcher._compute_deterministic_score(preferred_gap_job, role_family=ROLE_FAMILY_PM_CORE)

    assert required_scores["total"] <= 55
    assert preferred_scores["total"] > 55
