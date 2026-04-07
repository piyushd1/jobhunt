"""Playwright browser context manager with anti-detection and persistent sessions."""

import asyncio
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from playwright.async_api import BrowserContext, Page, async_playwright

import structlog

logger = structlog.get_logger()

# Actions that must NEVER be triggered automatically
BLOCKED_ACTIONS = {"send", "apply", "submit", "connect", "post", "confirm"}


@asynccontextmanager
async def browser_context(config: dict) -> AsyncGenerator[BrowserContext, None]:
    """Launch a persistent Playwright browser context with anti-detection flags."""
    browser_config = config.get("browser", {})
    profile_dir = str(Path(browser_config.get("profile_dir", "./data/browser_profile")).resolve())
    headless = browser_config.get("headless", False)
    slow_mo = browser_config.get("slow_mo", 800)

    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=headless,
            slow_mo=slow_mo,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        logger.info("browser_context_opened", profile_dir=profile_dir, headless=headless)
        try:
            yield ctx
        finally:
            await ctx.close()
            logger.info("browser_context_closed")


async def new_page(ctx: BrowserContext) -> Page:
    """Create a new page within the browser context."""
    page = await ctx.new_page()
    return page


async def safe_goto(page: Page, url: str, timeout: int = 30000) -> bool:
    """Navigate to a URL with timeout and error handling."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return True
    except Exception as e:
        logger.warning("page_navigation_failed", url=url, error=str(e))
        return False


async def human_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """Random delay to mimic human browsing behavior."""
    delay = random.uniform(min_s, max_s)
    await asyncio.sleep(delay)


async def random_scroll(page: Page, scrolls: int = 3) -> None:
    """Scroll the page randomly to mimic human reading."""
    for _ in range(scrolls):
        scroll_amount = random.randint(200, 600)
        await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
        await human_delay(0.5, 1.5)


def is_blocked_action(text: str) -> bool:
    """Check if a button/link text matches a blocked action."""
    return text.strip().lower() in BLOCKED_ACTIONS
