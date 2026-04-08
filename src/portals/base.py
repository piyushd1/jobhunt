"""Base portal adapter — interface that all portal scrapers implement."""

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Page

import structlog

logger = structlog.get_logger()


@dataclass
class RawJob:
    """A job discovered by a portal adapter — minimal fields before parsing."""
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    source: str = ""                     # Portal name
    experience_required: str = ""
    remote: str = ""
    snippet: str = ""                    # Short description from listing page
    posted_date: str = ""

    @property
    def fingerprint(self) -> str:
        """Dedup key: hash of normalized company + title + location."""
        raw = f"{self.company.lower().strip()}|{self.title.lower().strip()}|{self.location.lower().strip()}"
        return hashlib.md5(raw.encode()).hexdigest()


class PortalAdapter(ABC):
    """Base class for portal scrapers."""

    name: str = "base"
    base_url: str = ""

    def __init__(self, config: dict):
        portal_config = config.get("portals", {}).get(self.name, {})
        self.base_url = portal_config.get("base_url", self.base_url)
        self.search_config = config.get("search", {})
        self.max_results = self.search_config.get("max_results_per_portal", 75)
        self.pages_per_search = self.search_config.get("pages_per_search", 3)
        self.max_age_days = self.search_config.get("max_age_days", 15)

    def get_locations(self) -> list[str]:
        """Get search locations from config. Supports both single and multi-location."""
        locations = self.search_config.get("locations", [])
        if locations:
            return locations
        single = self.search_config.get("location", "India")
        return [single] if single else ["India"]

    @abstractmethod
    async def scrape(self, page: Page) -> list[RawJob]:
        """Scrape the portal for job listings. Returns list of RawJob."""
        ...

    async def health_check(self, page: Page) -> dict:
        """Check if portal is accessible and selectors still work.

        Returns: {"status": "ok"|"degraded"|"down", "details": "..."}
        """
        try:
            response = await page.goto(self.base_url, wait_until="domcontentloaded", timeout=15000)
            if response and response.status >= 400:
                return {"status": "down", "details": f"HTTP {response.status}"}

            # Check for login walls or CAPTCHAs
            content = await page.content()
            if self._detect_captcha(content):
                return {"status": "degraded", "details": "CAPTCHA detected"}
            if self._detect_login_wall(content):
                return {"status": "degraded", "details": "Login required"}

            return {"status": "ok", "details": ""}
        except Exception as e:
            return {"status": "down", "details": str(e)}

    @staticmethod
    def _detect_captcha(html: str) -> bool:
        captcha_patterns = [
            r"captcha", r"recaptcha", r"hcaptcha",
            r"verify.you.are.human", r"security.check",
        ]
        return any(re.search(p, html, re.IGNORECASE) for p in captcha_patterns)

    @staticmethod
    def _detect_login_wall(html: str) -> bool:
        login_patterns = [
            r"sign.?in.to.continue", r"log.?in.to.view",
            r"join.now.to.see", r"create.an.account",
        ]
        return any(re.search(p, html, re.IGNORECASE) for p in login_patterns)

    @staticmethod
    def extract_urls_from_text(text: str, domain_pattern: str = "") -> list[str]:
        """Fallback: extract job URLs via regex when CSS selectors fail."""
        url_pattern = r'https?://[^\s<>"\']+' + (domain_pattern or "")
        return list(set(re.findall(url_pattern, text)))
