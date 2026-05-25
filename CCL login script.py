#!/usr/bin/env python3
"""
Currency Cloud Direct - Login only
Usage: python ccl_login.py
"""

import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).parent / ".env"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

USERNAME = os.environ.get("CCL_USERNAME", "anubhavjain@tazapay.com")
PASSWORD = os.environ.get("CCL_PASSWORD", "Currencycloud@2026")
LOGIN_URL = "https://direct.currencycloud.com/login"

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


async def ss(page, name):
    path = f"ccl_dbg_{name}.png"
    await page.screenshot(path=path)
    print(f"  [ss] {path}")


async def dismiss_cookie_banner(page):
    await page.evaluate("""() => {
        const p = document.getElementById('CookieReportsPanel');
        const o = document.getElementById('CookieReportsOverlay');
        if (p) p.remove();
        if (o) o.remove();
    }""")


async def login(page) -> bool:
    print("[login] Opening login page ...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30_000)
    await page.wait_for_timeout(2_000)
    await dismiss_cookie_banner(page)
    await ss(page, "01_login_page")

    # Step 1: Login ID
    await page.locator('input[placeholder="Type your login ID"]').fill(USERNAME)
    await page.locator('button:has-text("Next")').click()
    await page.wait_for_timeout(2_500)
    await ss(page, "02_after_next")
    print(f"[login] URL: {page.url}")

    # Step 2: Password
    await page.locator('input[type="password"]').fill(PASSWORD)
    await page.locator('button:has-text("Login")').click()
    await page.wait_for_timeout(3_000)
    await ss(page, "03_after_password")
    print(f"[login] URL: {page.url}")

    # Step 3: Authy 2FA push
    if "two_step" in page.url:
        print("[2fa] Authy push sent — approve on your phone (waiting up to 120s) ...")
        await ss(page, "04_two_step")
        try:
            await page.wait_for_function(
                "() => !window.location.href.includes('two_step')",
                timeout=120_000
            )
            print("[2fa] Approved!")
        except PwTimeout:
            print("[2fa] WARNING: Timed out waiting for 2FA approval")
            return False

    await page.wait_for_timeout(2_000)
    await dismiss_cookie_banner(page)
    await ss(page, "05_dashboard")
    print(f"[login] Done. URL: {page.url}")
    return True


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=80,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
            locale="en-GB",
            timezone_id="Europe/London",
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()

        success = await login(page)
        if success:
            print("[+] Login successful")
            input("Press Enter to close browser ...")
        else:
            print("[!] Login failed")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
