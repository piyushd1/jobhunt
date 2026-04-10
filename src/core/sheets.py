"""Google Sheets writer — projects SQLite data into a Google Sheet."""

import json

import gspread
from google.oauth2.service_account import Credentials

import structlog

logger = structlog.get_logger()

FIT_BUCKET_ORDER = {
    "strong_fit": 0,
    "review_fit": 1,
    "weak_fit": 2,
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column headers — ordered by importance (user-facing first, metadata last)
JOBS_HEADERS = [
    "Title", "Company", "Location", "Remote",
    "Fit Bucket", "Match %", "Role Family",
    "Req Skill Score", "Pref Skill Score", "Domain Score", "Role Fit Score",
    "Exp Score", "Location Score",
    "Matched Skills", "Missing Skills", "Penalty Reasons", "Match Summary",
    "Portal Links", "Apply Link", "Experience",
    "Contact 1", "Contact 1 Title", "Contact 1 LinkedIn",
    "Contact 2", "Contact 2 Title", "Contact 2 LinkedIn",
    "Contact 3", "Contact 3 Title", "Contact 3 LinkedIn",
    "LinkedIn Draft 1", "LinkedIn Draft 2", "LinkedIn Draft 3",
    "Email Draft",
    "Status", "Notes",
    "Job ID", "Date Found", "Source(s)",
]

RUN_LOG_HEADERS = [
    "Run ID", "Started", "Completed", "Jobs Found", "Jobs Parsed",
    "Shortlisted", "Contacts Found", "Drafts Created", "Errors",
    "Duration", "LLM Cost ($)",
]


class SheetsWriter:
    """Write job hunt data to Google Sheets."""

    def __init__(self, credentials_path: str, sheet_id: str):
        self.sheet_id = sheet_id
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        self.gc = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(sheet_id)
        logger.info("sheets_connected", sheet_id=sheet_id)

    def _get_or_create_worksheet(self, title: str, headers: list[str]) -> gspread.Worksheet:
        """Get existing worksheet or create with headers. Always syncs headers."""
        try:
            ws = self.spreadsheet.worksheet(title)
            # Sync headers — update row 1 if headers changed
            existing_headers = ws.row_values(1)
            if existing_headers != headers:
                ws.update("A1", [headers])
                logger.info("worksheet_headers_updated", title=title)
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.update("A1", [headers])
            logger.info("worksheet_created", title=title)
        return ws

    def write_jobs(self, jobs: list[dict], contacts_by_job: dict, drafts_by_job: dict) -> int:
        """Write all jobs to the Jobs sheet in precision-review order."""
        ws = self._get_or_create_worksheet("Jobs", JOBS_HEADERS)

        # Clear existing data (keep header row)
        if ws.row_count > 1:
            ws.delete_rows(2, ws.row_count)

        # Sort by fit bucket first, then match score descending.
        sorted_jobs = sorted(
            jobs,
            key=lambda j: (
                FIT_BUCKET_ORDER.get(j.get("fit_bucket") or "", 99),
                -(j.get("match_score") or 0),
            ),
        )

        rows = []
        for job in sorted_jobs:
            contacts = contacts_by_job.get(job["id"], [])
            drafts = drafts_by_job.get(job["id"], [])

            # Format portal links — just URLs, no labels
            source_urls = _safe_json_loads(job.get("source_urls", "{}"), {})
            if source_urls:
                portal_links = "\n".join(source_urls.values())
            else:
                portal_links = job.get("url", "")

            # Format skills
            matched = _safe_json_loads(job.get("matched_skills", "[]"), [])
            missing = _safe_json_loads(job.get("missing_skills", "[]"), [])
            penalty_reasons = _safe_json_loads(job.get("penalty_reasons", "[]"), [])

            row = [
                # Core job info
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("remote", ""),
                # Scores
                job.get("fit_bucket") or "",
                job.get("match_score") or "",
                job.get("role_family") or job.get("role_family_hint") or "",
                job.get("required_skill_score") or "",
                job.get("preferred_skill_score") or "",
                job.get("domain_score") or "",
                job.get("role_fit_score") or "",
                job.get("experience_score") or "",
                job.get("location_score") or "",
                # Skills — NO truncation
                ", ".join(matched) if isinstance(matched, list) else str(matched),
                ", ".join(missing) if isinstance(missing, list) else str(missing),
                "\n".join(penalty_reasons) if isinstance(penalty_reasons, list) else str(penalty_reasons),
                # Match summary — generous limit
                _truncate(job.get("match_summary", ""), 1000),
                # Links
                portal_links,
                job.get("apply_url", ""),
                job.get("experience_required", ""),
            ]

            # Contacts (up to 3)
            for i in range(3):
                if i < len(contacts):
                    c = contacts[i]
                    row.extend([
                        c.get("name", ""),
                        c.get("title", ""),
                        c.get("linkedin_url", ""),
                    ])
                else:
                    row.extend(["", "", ""])

            # LinkedIn drafts (one per contact)
            contact_drafts = {d.get("contact_id"): d for d in drafts}
            for i in range(3):
                if i < len(contacts) and contacts[i]["id"] in contact_drafts:
                    row.append(contact_drafts[contacts[i]["id"]].get("linkedin_note", ""))
                else:
                    row.append("")

            # Email draft
            email_draft = next((d.get("email_body", "") for d in drafts if d.get("email_body")), "")
            row.append(email_draft)

            # Status + notes + metadata (at the end)
            row.extend([
                job.get("status", "new"),
                job.get("notes", ""),
                job.get("id", ""),
                (job.get("created_at") or "")[:10],  # Date only
                job.get("source", ""),
            ])

            rows.append(row)

        if rows:
            ws.append_rows(rows, value_input_option="RAW")
            logger.info("jobs_written_to_sheets", count=len(rows))

        return len(rows)

    def write_run_log(self, run: dict) -> None:
        """Append a run log entry."""
        ws = self._get_or_create_worksheet("Run Log", RUN_LOG_HEADERS)
        row = [
            run.get("run_id", run.get("id", "")),
            run.get("started_at", ""),
            run.get("completed_at", ""),
            run.get("jobs_found", 0),
            run.get("jobs_parsed", 0),
            run.get("jobs_shortlisted", 0),
            run.get("contacts_found", 0),
            run.get("drafts_created", 0),
            run.get("errors", 0),
            run.get("duration_s", run.get("duration", "")),
            run.get("llm_cost", 0.0),
        ]
        ws.append_row(row)
        logger.info("run_log_written", run_id=run.get("run_id", run.get("id")))


def _truncate(text: str, max_len: int) -> str:
    if not text:
        return ""
    return text[:max_len] if len(text) <= max_len else text[:max_len - 3] + "..."


def _safe_json_loads(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value if value else default
