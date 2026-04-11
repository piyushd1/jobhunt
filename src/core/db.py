"""SQLite database operations — source of truth for all job hunt data."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    fingerprint TEXT UNIQUE,
    source TEXT,
    source_urls TEXT,              -- JSON: {"LinkedIn": "url1", "Naukri": "url2"}
    apply_url TEXT,
    url TEXT NOT NULL,
    title TEXT,
    company TEXT,
    location TEXT,
    remote TEXT,
    snippet TEXT,
    posted_date TEXT,
    experience_required TEXT,
    skills_required TEXT,          -- JSON array
    required_skills TEXT,          -- JSON array
    preferred_skills TEXT,         -- JSON array
    full_description TEXT,
    jd_summary TEXT,
    match_score REAL,
    skill_score REAL,
    required_skill_score REAL,
    preferred_skill_score REAL,
    experience_score REAL,
    location_score REAL,
    domain_score REAL,
    role_fit_score REAL,
    matched_skills TEXT,           -- JSON array
    missing_skills TEXT,           -- JSON array
    match_summary TEXT,
    role_family_hint TEXT,
    role_family TEXT,
    fit_bucket TEXT,
    penalty_reasons TEXT,          -- JSON array
    status TEXT DEFAULT 'new',
    parse_status TEXT DEFAULT 'pending',  -- pending/parsed/failed
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    name TEXT,
    title TEXT,
    linkedin_url TEXT,
    relevance_reason TEXT,
    confidence TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    contact_id TEXT REFERENCES contacts(id),
    job_id TEXT REFERENCES jobs(id),
    linkedin_note TEXT,
    email_subject TEXT,
    email_body TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    jobs_found INTEGER DEFAULT 0,
    jobs_parsed INTEGER DEFAULT 0,
    jobs_shortlisted INTEGER DEFAULT 0,
    contacts_found INTEGER DEFAULT 0,
    drafts_created INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    config_snapshot TEXT
);

CREATE TABLE IF NOT EXISTS agent_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    agent TEXT,
    items_in INTEGER,
    items_out INTEGER,
    success_rate REAL,
    avg_duration_ms REAL,
    errors TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    agent TEXT,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS blacklist (
    id TEXT PRIMARY KEY,
    type TEXT,
    value TEXT,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

JOB_COLUMN_MIGRATIONS = {
    "snippet": "ALTER TABLE jobs ADD COLUMN snippet TEXT",
    "posted_date": "ALTER TABLE jobs ADD COLUMN posted_date TEXT",
    "required_skills": "ALTER TABLE jobs ADD COLUMN required_skills TEXT",
    "preferred_skills": "ALTER TABLE jobs ADD COLUMN preferred_skills TEXT",
    "required_skill_score": "ALTER TABLE jobs ADD COLUMN required_skill_score REAL",
    "preferred_skill_score": "ALTER TABLE jobs ADD COLUMN preferred_skill_score REAL",
    "domain_score": "ALTER TABLE jobs ADD COLUMN domain_score REAL",
    "role_fit_score": "ALTER TABLE jobs ADD COLUMN role_fit_score REAL",
    "role_family_hint": "ALTER TABLE jobs ADD COLUMN role_family_hint TEXT",
    "role_family": "ALTER TABLE jobs ADD COLUMN role_family TEXT",
    "fit_bucket": "ALTER TABLE jobs ADD COLUMN fit_bucket TEXT",
    "penalty_reasons": "ALTER TABLE jobs ADD COLUMN penalty_reasons TEXT",
}


class Database:
    """SQLite database wrapper for job hunt data."""

    VALID_JOB_FIELDS = {
        "id", "fingerprint", "source", "source_urls", "apply_url", "url",
        "title", "company", "location", "remote", "snippet", "posted_date",
        "experience_required", "skills_required", "required_skills", "preferred_skills",
        "full_description", "jd_summary", "match_score", "skill_score",
        "required_skill_score", "preferred_skill_score", "experience_score",
        "location_score", "domain_score", "role_fit_score", "matched_skills",
        "missing_skills", "match_summary", "role_family_hint", "role_family",
        "fit_bucket", "penalty_reasons", "status", "parse_status", "notes",
        "created_at", "updated_at"
    }

    VALID_RUN_FIELDS = {
        "id", "started_at", "completed_at", "jobs_found", "jobs_parsed",
        "jobs_shortlisted", "contacts_found", "drafts_created", "errors",
        "config_snapshot"
    }

    def __init__(self, db_path: str = "./data/job_hunt.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        logger.info("database_initialized", path=db_path)

    def _init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._ensure_job_columns()
        self.conn.commit()

    def _ensure_job_columns(self) -> None:
        """Apply additive migrations for existing databases."""
        existing = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for column, ddl in JOB_COLUMN_MIGRATIONS.items():
            if column not in existing:
                self.conn.execute(ddl)

    def close(self) -> None:
        self.conn.close()

    # --- Jobs ---

    def insert_job(self, job: dict) -> bool:
        """Insert a new job. Returns False if duplicate (by fingerprint)."""
        try:
            self.conn.execute(
                """INSERT INTO jobs (
                   id, fingerprint, source, source_urls, apply_url, url,
                   title, company, location, remote, snippet, posted_date,
                   experience_required, skills_required, required_skills, preferred_skills,
                   full_description, jd_summary, role_family_hint, status, parse_status
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job["id"], job["fingerprint"], job.get("source", ""),
                    json.dumps(job.get("source_urls", {})),
                    job.get("apply_url", ""), job["url"],
                    job.get("title", ""), job.get("company", ""),
                    job.get("location", ""), job.get("remote", ""),
                    job.get("snippet", ""), job.get("posted_date", ""),
                    job.get("experience_required", ""),
                    json.dumps(job.get("skills_required", [])),
                    json.dumps(job.get("required_skills", [])),
                    json.dumps(job.get("preferred_skills", [])),
                    job.get("full_description", ""), job.get("jd_summary", ""),
                    job.get("role_family_hint", ""),
                    job.get("status", "new"), job.get("parse_status", "pending"),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def merge_job_source(self, fingerprint: str, source: str, url: str) -> None:
        """Merge a new source URL into an existing job's sources."""
        row = self.conn.execute(
            "SELECT source, source_urls FROM jobs WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if not row:
            return

        existing_sources = set(row["source"].split("; ")) if row["source"] else set()
        existing_sources.add(source)

        existing_urls = json.loads(row["source_urls"]) if row["source_urls"] else {}
        existing_urls[source] = url

        self.conn.execute(
            "UPDATE jobs SET source = ?, source_urls = ?, updated_at = ? WHERE fingerprint = ?",
            ("; ".join(sorted(existing_sources)), json.dumps(existing_urls),
             datetime.utcnow().isoformat(), fingerprint),
        )
        self.conn.commit()

    def update_job(self, job_id: str, **fields) -> None:
        """Update specific fields on a job."""
        if not fields:
            return

        # Validate fields against whitelist to prevent SQL injection
        invalid_fields = set(fields.keys()) - self.VALID_JOB_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid fields for jobs table: {invalid_fields}")

        # Serialize JSON fields
        for key in (
            "skills_required",
            "required_skills",
            "preferred_skills",
            "matched_skills",
            "missing_skills",
            "penalty_reasons",
            "source_urls",
        ):
            if key in fields and isinstance(fields[key], (list, dict)):
                fields[key] = json.dumps(fields[key])
        fields["updated_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [job_id]
        self.conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", values)
        self.conn.commit()

    def get_jobs(self, status: Optional[str] = None, parse_status: Optional[str] = None,
                 min_score: Optional[float] = None, limit: int = 250) -> list[dict]:
        """Query jobs with optional filters."""
        query = "SELECT * FROM jobs WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if parse_status:
            query += " AND parse_status = ?"
            params.append(parse_status)
        if min_score is not None:
            query += " AND match_score >= ?"
            params.append(min_score)
        query += " ORDER BY match_score DESC NULLS LAST, created_at DESC LIMIT ?"
        params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def job_exists(self, fingerprint: str) -> bool:
        """Check if a job with this fingerprint already exists."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    # --- Contacts ---

    def insert_contact(self, contact: dict) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO contacts (id, job_id, name, title, linkedin_url,
               relevance_reason, confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (contact["id"], contact["job_id"], contact.get("name", ""),
             contact.get("title", ""), contact.get("linkedin_url", ""),
             contact.get("relevance_reason", ""), contact.get("confidence", "medium")),
        )
        self.conn.commit()

    def get_contacts_for_job(self, job_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE job_id = ?", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Drafts ---

    def insert_draft(self, draft: dict) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO drafts (id, contact_id, job_id, linkedin_note,
               email_subject, email_body)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (draft["id"], draft["contact_id"], draft["job_id"],
             draft.get("linkedin_note", ""), draft.get("email_subject", ""),
             draft.get("email_body", "")),
        )
        self.conn.commit()

    def get_drafts_for_job(self, job_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM drafts WHERE job_id = ?", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Runs ---

    def insert_run(self, run: dict) -> None:
        self.conn.execute(
            """INSERT INTO runs (id, started_at, config_snapshot)
               VALUES (?, ?, ?)""",
            (run["id"], run["started_at"], json.dumps(run.get("config_snapshot", {}))),
        )
        self.conn.commit()

    def update_run(self, run_id: str, **fields) -> None:
        if not fields:
            return

        # Validate fields against whitelist to prevent SQL injection
        invalid_fields = set(fields.keys()) - self.VALID_RUN_FIELDS
        if invalid_fields:
            raise ValueError(f"Invalid fields for runs table: {invalid_fields}")

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [run_id]
        self.conn.execute(f"UPDATE runs SET {set_clause} WHERE id = ?", values)
        self.conn.commit()

    # --- Agent Metrics ---

    def log_agent_metrics(self, run_id: str, agent: str, items_in: int, items_out: int,
                          success_rate: float, avg_duration_ms: float,
                          errors: Optional[list] = None) -> None:
        self.conn.execute(
            """INSERT INTO agent_metrics (run_id, agent, items_in, items_out, success_rate,
               avg_duration_ms, errors)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, agent, items_in, items_out, success_rate, avg_duration_ms,
             json.dumps(errors or [])),
        )
        self.conn.commit()

    # --- Cost Log ---

    def log_cost(self, run_id: str, agent: str, model: str,
                 input_tokens: int, output_tokens: int, cost_usd: float) -> None:
        self.conn.execute(
            """INSERT INTO cost_log (run_id, agent, model, input_tokens, output_tokens, cost_usd)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, agent, model, input_tokens, output_tokens, cost_usd),
        )
        self.conn.commit()

    # --- Blacklist ---

    def get_blacklist(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM blacklist").fetchall()
        return [dict(r) for r in rows]

    def add_to_blacklist(self, bl_id: str, bl_type: str, value: str, reason: str = "") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO blacklist (id, type, value, reason) VALUES (?, ?, ?, ?)",
            (bl_id, bl_type, value, reason),
        )
        self.conn.commit()
