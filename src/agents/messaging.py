"""Messaging Agent — drafts personalized outreach for each contact.

For each contact found by lead gen, generates:
- LinkedIn connection note (300 char max)
- Email draft (80-140 words)

Uses resume RAG to reference specific relevant projects.
Never invents facts or exaggerates.
"""

import asyncio
import json
import uuid
from typing import Any

import structlog

from src.agents.base import AgentResult, BaseAgent
from src.core.db import Database
from src.core.llm import LLMClient
from src.core.vectorstore import ResumeVectorStore

logger = structlog.get_logger()

MESSAGING_SYSTEM = """You are a professional outreach message writer helping a job seeker ask for referrals.

The goal is to connect with people at the target company and ask if they'd be open to referring the candidate, or helping them understand the role better.

Tone rules:
- NEVER invent facts or exaggerate credentials
- NEVER claim to know the contact personally
- Be genuine, warm, and respectful of their time
- Acknowledge that you're reaching out cold
- Reference ONE specific, real project or skill from the candidate that's relevant
- Adapt based on contact's role:
  - Recruiters: ask if the role is still open, express strong interest, mention relevant experience
  - Hiring managers / PMs: mention a specific project that maps to their team's work, ask for a referral or quick chat
  - HR: ask about the hiring process and if a referral would help
  - Leadership: be brief, mention a high-level achievement, ask if they can point you to the right person
- Always make the ASK clear: you're looking for a referral, a warm intro, or a quick chat about the role

Return ONLY valid JSON:
{
  "linkedin_note": "Connection note, max 300 characters. Be warm, mention one relevant thing, and make a clear ask (referral/chat).",
  "email_subject": "Short subject line — direct and non-spammy",
  "email_body": "80-140 word email. Open with why you're reaching out. One sentence about your relevant background. Clear ask: would they be open to referring you, or a 10-min call? Close warmly."
}"""


class MessagingAgent(BaseAgent):
    """Draft personalized outreach messages for contacts."""

    name = "messaging"

    def __init__(self, config: dict, db: Database, llm: LLMClient,
                 vectorstore: ResumeVectorStore, profile: dict):
        super().__init__(config)
        self.db = db
        self.llm = llm
        self.vectorstore = vectorstore
        self.profile = profile
        self.delay = config.get("llm", {}).get("delay_between_calls_s", 2)

    async def run(self, input_data: Any = None) -> AgentResult:
        """Generate drafts for all contacts that don't have them yet."""
        # Find contacts without drafts
        all_jobs = self.db.get_jobs(parse_status="parsed")
        contacts_to_draft = []

        for job in all_jobs:
            contacts = self.db.get_contacts_for_job(job["id"])
            for contact in contacts:
                existing_drafts = self.db.get_drafts_for_job(job["id"])
                drafted_contact_ids = {d.get("contact_id") for d in existing_drafts}
                if contact["id"] not in drafted_contact_ids:
                    contacts_to_draft.append((job, contact))

        if not contacts_to_draft:
            logger.info("messaging_nothing_to_draft")
            return AgentResult(data=[], count=0)

        logger.info("messaging_start", contacts=len(contacts_to_draft))
        drafted = 0
        errors: list[str] = []

        for i, (job, contact) in enumerate(contacts_to_draft):
            try:
                draft = await self._generate_draft(job, contact)
                draft["id"] = str(uuid.uuid4())[:8]
                draft["contact_id"] = contact["id"]
                draft["job_id"] = job["id"]
                self.db.insert_draft(draft)
                drafted += 1

                logger.info("messaging_draft_done",
                            progress=f"{i+1}/{len(contacts_to_draft)}",
                            contact=contact.get("name", "")[:30])

            except Exception as e:
                errors.append(f"{contact.get('name', 'unknown')}: {str(e)}")
                logger.warning("messaging_draft_failed",
                               contact=contact.get("name"), error=str(e))

            await asyncio.sleep(self.delay)

        logger.info("messaging_complete", drafted=drafted, errors=len(errors))
        return AgentResult(
            data={"drafted": drafted},
            count=drafted,
            errors=errors,
        )

    async def _generate_draft(self, job: dict, contact: dict) -> dict:
        """Generate LinkedIn note + email draft for one contact."""
        # Get relevant resume chunks for this job
        jd_text = job.get("full_description") or job.get("jd_summary") or ""
        relevant_chunks = self.vectorstore.query(jd_text, top_k=2)
        context = "\n".join(f"- {c['text']}" for c in relevant_chunks)

        candidate_name = self.profile.get("name", "the candidate")

        prompt = f"""Candidate: {candidate_name}
Candidate background:
{context}

Job: {job.get('title', '')} at {job.get('company', '')}
Location: {job.get('location', '')}
Match score: {job.get('match_score', 'N/A')}%

Contact: {contact.get('name', '')}
Contact title: {contact.get('title', '')}
Contact LinkedIn: {contact.get('linkedin_url', '')}
How found: {contact.get('relevance_reason', '')}

Write a LinkedIn connection note (max 300 chars) and a short email (80-140 words).
Reference specific skills or experience that are relevant to this role."""

        result = await self.llm.complete_json(
            prompt=prompt,
            system=MESSAGING_SYSTEM,
            agent=self.name,
        )

        # Enforce LinkedIn note length
        linkedin_note = result.get("linkedin_note", "")
        if len(linkedin_note) > 300:
            linkedin_note = linkedin_note[:297] + "..."

        return {
            "linkedin_note": linkedin_note,
            "email_subject": result.get("email_subject", ""),
            "email_body": result.get("email_body", ""),
        }
