# Codex Change Log

This file summarizes the precision-first PM match upgrade implemented in this pass.

## Product and Ranking Changes

- Added precision-first role-family classification for `pm_core`, `tpm_pgm`, `adjacent`, and `other`.
- Tightened sourcing so default ingestion now keeps PM core plus TPM/PgM roles and skips broad adjacent roles like Project Manager, Business Operations, Chief of Staff, and Growth Manager.
- Made fallback-job dedupe safer by deriving fingerprints from stable URL/job identifiers when title/company metadata is sparse.
- Preserved portal `snippet` and `posted_date` fields when available.
- Updated parsing to keep `required_skills` and `preferred_skills` separate while still deriving the legacy combined `skills_required` list.
- Added `role_family_hint` generation during parsing to help downstream PM-vs-TPM/PgM scoring.
- Replaced the older broad hybrid scorer with a precision-first model:
  - `required_skill_score`
  - `preferred_skill_score`
  - `experience_score`
  - `location_score`
  - `domain_score`
  - `role_fit_score`
- Added domain-risk caps so low-fit domains like pure B2B SaaS, core banking, insurance, hardware, and telecom remain visible but are strongly penalized instead of hard-filtered out.
- Added persisted ranking artifacts:
  - `role_family`
  - `fit_bucket`
  - `penalty_reasons`
  - separate required/preferred skill scores
  - domain and role-fit scores

## Data and Output Changes

- Extended the SQLite `jobs` schema with additive migrations so existing local databases move forward safely.
- Updated reset logic to clear the new scoring fields cleanly.
- Updated Google Sheets output to expose:
  - `Fit Bucket`
  - `Role Family`
  - required/preferred skill scores
  - `Domain Score`
  - `Role Fit Score`
  - `Penalty Reasons`
- Changed sheet ordering to `fit_bucket` first and score second.
- Raised the default sheet projection threshold from `50` to `60`.

## Config and Docs

- Added explicit precision-first config controls:
  - `search.allowed_role_families`
  - `search.excluded_title_keywords`
  - `matching.precision_mode`
  - `matching.weights.required_skills`
  - `matching.weights.preferred_skills`
  - `matching.weights.role_fit`
- Narrowed the default search keywords to PM core plus TPM/PgM.
- Updated `README.md` and `CLAUDE.md` to reflect the new scoring model and default operating mode.
- Added `rich` to runtime dependencies because the CLI uses it directly.
- Fixed `.gitignore` so sensitive local files stay local and `CLAUDE.md` can be versioned.

## Tests and Verification

- Added focused tests for:
  - sparse-card dedupe fallback
  - role-family gating
  - parsing required vs preferred skills
  - PM core outranking adjacent roles
  - TPM/PgM staying eligible
  - B2B SaaS and core banking soft penalties
  - mandatory-skill caps applying only to required skills
  - CLI import smoke
- Verified with:
  - `PYTHONPYCACHEPREFIX=/tmp .venv/bin/python -m pytest -q`
  - `PYTHONPYCACHEPREFIX=/tmp .venv/bin/python -m src config`

## Suggested Next Step

- Next high-leverage work is evals: build offline fixtures and score audits from real historical jobs so the new weights, buckets, and domain caps can be tuned against observed results rather than heuristics alone.
