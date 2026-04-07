"""One-time browser setup — log into all job portals in a persistent profile.

Run this once before using the pipeline:
    python setup_browser.py

It opens each portal's login page and waits for you to log in manually.
Cookies are saved in the persistent browser profile for reuse.
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PORTALS = [
    ("LinkedIn",    "https://www.linkedin.com/login"),
    ("Naukri",      "https://www.naukri.com/nlogin/login"),
    ("Foundit",     "https://www.foundit.in/login"),
    ("Indeed",      "https://secure.indeed.com/account/login"),
    ("Instahyre",   "https://www.instahyre.com/login/"),
    ("Hirist",      "https://www.hirist.tech/login"),
    ("Wellfound",   "https://wellfound.com/login"),
    ("WhatsApp",    "https://web.whatsapp.com"),
]

PROFILE_DIR = "./data/browser_profile"


async def setup():
    profile_path = Path(PROFILE_DIR).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    print(f"\nBrowser profile directory: {profile_path}")
    print("=" * 60)
    print("Log into each portal when prompted.")
    print("Press ENTER after logging in to move to the next portal.")
    print("Type 'skip' to skip a portal.")
    print("=" * 60)

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_path),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        page = await ctx.new_page()

        for name, url in PORTALS:
            print(f"\n-> {name}: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"   Warning: Could not load {name} ({e})")

            response = input("   Press ENTER when logged in (or type 'skip'): ").strip().lower()
            if response == "skip":
                print(f"   Skipped {name}")
                continue
            print(f"   {name} session saved.")

        await ctx.close()
        print("\nAll sessions saved. You can now run the pipeline.")


if __name__ == "__main__":
    asyncio.run(setup())
