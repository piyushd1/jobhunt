"""One-time browser setup — log into all job portals in a persistent profile.

Run before using the pipeline:
    python setup_browser.py

For each portal: opens a NEW tab, navigates to the login page, and waits
for you to log in manually. Tabs stay open so you can come back to them.
Cookies are saved in the persistent browser profile for reuse.

Tip: do NOT close the browser window mid-setup. If you do, the script
will detect it and reopen the context.
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

PORTALS = [
    ("LinkedIn",   "https://www.linkedin.com/login"),
    ("Naukri",     "https://www.naukri.com/nlogin/login"),
    ("Foundit",    "https://www.foundit.in/login"),
    ("Indeed",     "https://secure.indeed.com/account/login"),
    ("Instahyre",  "https://www.instahyre.com/login/"),
    ("Hirist",     "https://www.hirist.tech/login"),
    ("Wellfound",  "https://wellfound.com/login"),
    ("WhatsApp",   "https://web.whatsapp.com"),
]

PROFILE_DIR = "./data/browser_profile"


async def _launch_context(pw, profile_path: Path) -> BrowserContext:
    """Launch a persistent Chromium context with anti-detection flags."""
    return await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_path),
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )


async def _ensure_context(pw, profile_path: Path,
                          ctx: BrowserContext) -> BrowserContext:
    """Re-launch the context if it has been closed (e.g., user closed window)."""
    try:
        # Probe — if context is alive, this returns the pages list cheaply.
        _ = ctx.pages
        return ctx
    except Exception:
        print("   (Context closed — relaunching browser…)")
        return await _launch_context(pw, profile_path)


async def _open_in_new_tab(ctx: BrowserContext, url: str) -> tuple[Page, str]:
    """Open `url` in a fresh tab. Returns (page, error_str)."""
    try:
        page = await ctx.new_page()
    except Exception as e:
        return None, f"could not open new tab: {e}"

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return page, ""
    except Exception as e:
        return page, str(e)


async def setup():
    profile_path = Path(PROFILE_DIR).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    # Clean stale lock files from a prior crashed/killed Chromium.
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock_path = profile_path / lock_name
        if lock_path.exists() or lock_path.is_symlink():
            try:
                lock_path.unlink()
            except OSError:
                pass

    print(f"\nBrowser profile: {profile_path}")
    print("=" * 64)
    print("A fresh tab will open for each portal. Log in, then come back")
    print("here and press ENTER to move to the next one.")
    print("Type 'skip' to skip a portal. Don't close the browser window.")
    print("=" * 64)

    async with async_playwright() as pw:
        ctx = await _launch_context(pw, profile_path)

        # Close the auto-opened blank tab Chromium creates on first launch.
        for p in list(ctx.pages):
            try:
                await p.close()
            except Exception:
                pass

        try:
            for name, url in PORTALS:
                print(f"\n→ {name}: {url}")
                ctx = await _ensure_context(pw, profile_path, ctx)

                page, err = await _open_in_new_tab(ctx, url)
                if err:
                    print(f"   Warning: could not load {name} ({err[:120]})")
                if page is None:
                    # New-tab itself failed — context likely dead. Reopen and retry once.
                    ctx = await _launch_context(pw, profile_path)
                    page, err = await _open_in_new_tab(ctx, url)
                    if err:
                        print(f"   Retry failed for {name}: {err[:120]}")

                response = input(
                    "   Press ENTER when logged in (or type 'skip'): "
                ).strip().lower()
                if response == "skip":
                    print(f"   Skipped {name}")
                else:
                    print(f"   {name} session saved.")

            print("\nAll sessions saved. You can now run the pipeline.")
        finally:
            try:
                await ctx.close()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(setup())
