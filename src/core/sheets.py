"""Google Sheets writer — projects SQLite data into a Google Sheet."""

import json
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

import structlog

logger = structlog.get_logger()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column headers for the Jobs sheet
JOBS_HEADERS = [
    "Job ID", "Date Found", "Source(s)", "Title", "Company", "Location",
    "Remote", "Experience", "Portal Links", "Apply Link",
    "Match %", "Skill Score", "Exp Score", "Location Score",
    "Matched Skills", "Missing Skills", "Match Summary",
    "Contact 1 Name", "Contact 1 Title", "Contact 1 LinkedIn",
    "Contact 2 Name", "Contact 2 Title", "Contact 2 LinkedIn",
    "Contact 3 Name", "Contact 3 Title", "Contact 3 LinkedIn",
    "LinkedIn Draft 1", "LinkedIn Draft 2", "LinkedIn Draft 3",
    "Email Draft",
    "Status", "Notes",
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
        """Get existing worksheet or create with headers."""
        try:
            ws = self.spreadsheet.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.append_row(headers)
            logger.info("worksheet_created", title=title)
        return ws

    def write_jobs(self, jobs: list[dict], contacts_by_job: dict, drafts_by_job: dict) -> int:
        """Write all jobs to the Jobs sheet. Returns number of rows written."""
        ws = self._get_or_create_worksheet("Jobs", JOBS_HEADERS)

        # Clear existing data (keep header)
        if ws.row_count > 1:
            ws.delete_rows(2, ws.row_count)

        rows = []
        for job in jobs:
            contacts = contacts_by_job.get(job["id"], [])
            drafts = drafts_by_job.get(job["id"], [])

            # Format portal links
            source_urls = json.loads(job.get("source_urls", "{}")) if job.get("source_urls") else {}
            portal_links = "\n".join(f"{k}: {v}" for k, v in source_urls.items()) if source_urls else job.get("url", "")

            # Format skills as comma-separated
            matched = _parse_json_field(job.get("matched_skills", "[]"))
            missing = _parse_json_field(job.get("missing_skills", "[]"))

            row = [
                job.get("id", ""),
                job.get("created_at", ""),
                job.get("source", ""),
                job.get("title", ""),
                job.get("company", ""),
                job.get("location", ""),
                job.get("remote", ""),
                job.get("experience_required", ""),
                portal_links,
                job.get("apply_url", ""),
                job.get("match_score", ""),
                job.get("skill_score", ""),
                job.get("experience_score", ""),
                job.get("location_score", ""),
                ", ".join(matched) if isinstance(matched, list) else str(matched),
                ", ".join(missing) if isinstance(missing, list) else str(missing),
                _truncate(job.get("match_summary", ""), 300),
            ]

            # Add up to 3 contacts
            for i in range(3):
                if i < len(contacts):
                    c = contacts[i]
                    row.extend([c.get("name", ""), c.get("title", ""), c.get("linkedin_url", "")])
                else:
                    row.extend(["", "", ""])

            # Add drafts for each contact
            contact_drafts = {d.get("contact_id"): d for d in drafts}
            for i in range(3):
                if i < len(contacts) and contacts[i]["id"] in contact_drafts:
                    row.append(_truncate(contact_drafts[contacts[i]["id"]].get("linkedin_note", ""), 300))
                else:
                    row.append("")

            # Email draft (first one found)
            email_draft = next((d.get("email_body", "") for d in drafts if d.get("email_body")), "")
            row.append(_truncate(email_draft, 500))

            row.extend([job.get("status", "new"), job.get("notes", "")])
            rows.append(row)

        if rows:
            ws.append_rows(rows, value_input_option="RAW")
            logger.info("jobs_written_to_sheets", count=len(rows))

        return len(rows)

    def write_run_log(self, run: dict) -> None:
        """Append a run log entry."""
        ws = self._get_or_create_worksheet("Run Log", RUN_LOG_HEADERS)
        row = [
            run.get("id", ""),
            run.get("started_at", ""),
            run.get("completed_at", ""),
            run.get("jobs_found", 0),
            run.get("jobs_parsed", 0),
            run.get("jobs_shortlisted", 0),
            run.get("contacts_found", 0),
            run.get("drafts_created", 0),
            run.get("errors", 0),
            run.get("duration", ""),
            run.get("llm_cost", 0.0),
        ]
        ws.append_row(row)
        logger.info("run_log_written", run_id=run.get("id"))


def _truncate(text: str, max_len: int) -> str:
    if not text:
        return ""
    return text[:max_len] + "..." if len(text) > max_len else text


def _parse_json_field(value: str):
    if not value:
        return []
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
