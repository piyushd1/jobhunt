from src.core.roles import (
    ROLE_FAMILY_ADJACENT,
    ROLE_FAMILY_PM_CORE,
    ROLE_FAMILY_TPM_PGM,
    classify_role_family,
    is_allowed_role,
)
from src.portals.base import RawJob


def test_raw_job_fingerprint_falls_back_to_url_when_metadata_is_sparse():
    job_a = RawJob(
        url="https://www.linkedin.com/jobs/view/123456789/?trk=public_jobs_topcard-title",
        source="LinkedIn",
    )
    job_b = RawJob(
        url="https://www.linkedin.com/jobs/view/987654321/?trk=public_jobs_topcard-title",
        source="LinkedIn",
    )

    assert job_a.fingerprint != job_b.fingerprint


def test_role_gate_allows_pm_and_tpm_but_blocks_adjacent_roles():
    assert classify_role_family("Senior Product Manager") == ROLE_FAMILY_PM_CORE
    assert classify_role_family("Technical Program Manager") == ROLE_FAMILY_TPM_PGM
    assert classify_role_family("Business Operations Manager") == ROLE_FAMILY_ADJACENT

    assert is_allowed_role("Senior Product Manager", "Marketplace growth")
    assert is_allowed_role("Technical Program Manager", "Platform APIs")
    assert not is_allowed_role("Project Manager", "General PMO")
    assert not is_allowed_role("Chief of Staff", "Strategy role")
