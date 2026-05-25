#!/usr/bin/env python3
"""
Currency Cloud Direct - Transactions Report Export -> S3 -> Slack

Flow:
  1. Login  (Login ID -> Next -> Password -> Login -> Authy 2FA push)
  2. Reports (sidebar) -> Transactions Report
  3. Set Filter by = Created at
  4. Pick start / end dates via calendar
  5. Open Advanced filter -> Status dropdown -> select Deleted (makes Status = ALL)
  6. Download CSV
  7. Upload to S3  (optional, set S3_ENABLED=true)
  8. Slack DM notification

Usage:
    python ccl_export.py
    python ccl_export.py --start_date 2026-05-01 --end_date 2026-05-24
"""

import argparse
import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path

import boto3
import requests
from playwright.async_api import async_playwright, TimeoutError as PwTimeout
from playwright_stealth import stealth, Stealth

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

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser()
_parser.add_argument("--start_date", type=str, default=None)
_parser.add_argument("--end_date",   type=str, default=None)
_args = _parser.parse_args()

def _yesterday():
    return (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

START_DATE = _args.start_date or os.environ.get("CCL_START_DATE", _yesterday())
END_DATE   = _args.end_date   or os.environ.get("CCL_END_DATE",   _yesterday())

START_DT = datetime.strptime(START_DATE, "%Y-%m-%d")
END_DT   = datetime.strptime(END_DATE,   "%Y-%m-%d")

def _file_date(d): return d.strftime("%d%m%Y")

FILENAME = f"CCL_TRANSACTIONS_{_file_date(START_DT)}_to_{_file_date(END_DT)}.csv"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
USERNAME = os.environ.get("CCL_USERNAME", "")
PASSWORD = os.environ.get("CCL_PASSWORD", "")
IS_CI    = os.environ.get("CI", "false").lower() == "true"

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_USER_ID   = os.environ.get("SLACK_USER_ID", "")

S3_ENABLED = os.environ.get("S3_ENABLED", "false").lower() == "true"
S3_BUCKET  = os.environ.get("S3_BUCKET", "payout-recon")
S3_PREFIX  = os.environ.get("S3_CCL_TRANSACTIONS_PREFIX", "ccl/transactions/raw/")
S3_REGION  = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")

DOWNLOAD_DIR = Path("downloads")
LOGIN_URL    = "https://direct.currencycloud.com/login"

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


CCL_NOTIFY_CHANNEL = "D0B63FWCUCC"   # DM channel: cclautomationbot -> anubhavjain@tazapay.com


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def notify_slack_ccl(message: str):
    """Send a plain-text message to the CCL notification DM channel."""
    if not SLACK_BOT_TOKEN:
        print(f"[slack-ccl] Not configured — skipping: {message}")
        return
    try:
        headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                   "Content-Type": "application/json"}
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": CCL_NOTIFY_CHANNEL, "text": message},
            headers=headers, timeout=10,
        )
        ok = r.json().get("ok")
        print(f"[slack-ccl] {'Sent' if ok else 'Error: ' + str(r.json().get('error'))}: {message}")
    except Exception as e:
        print(f"[slack-ccl] Failed: {e}")


async def ss(page, name):
    path = f"ccl_dbg_{name}.png"
    await page.screenshot(path=path)
    print(f"  [ss] {path}")


async def dismiss_cookie_banner(page):
    try:
        await page.evaluate("""() => {
            const p = document.getElementById('CookieReportsPanel');
            const o = document.getElementById('CookieReportsOverlay');
            if (p) p.remove();
            if (o) o.remove();
        }""")
    except Exception:
        pass


def upload_to_s3(local_path: Path) -> str:
    s3_key = f"{S3_PREFIX}{local_path.name}"
    print(f"[s3] Uploading -> s3://{S3_BUCKET}/{s3_key}")
    s3 = boto3.client(
        "s3", region_name=S3_REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    s3.upload_file(str(local_path), S3_BUCKET, s3_key,
                   ExtraArgs={"ContentType": "text/csv"})
    uri = f"s3://{S3_BUCKET}/{s3_key}"
    print(f"[s3] Done -> {uri}")
    return uri


def notify_slack(message: str, color: str = "good"):
    if not SLACK_BOT_TOKEN or not SLACK_USER_ID:
        print("[slack] Not configured — skipping.")
        return
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}",
               "Content-Type": "application/json"}
    channel = SLACK_USER_ID
    if SLACK_USER_ID.startswith("U"):
        try:
            r = requests.post("https://slack.com/api/conversations.open",
                              json={"users": SLACK_USER_ID}, headers=headers, timeout=10)
            if r.json().get("ok"):
                channel = r.json()["channel"]["id"]
        except Exception:
            pass
    icon = {"good": ":white_check_mark:", "warning": ":warning:", "danger": ":x:"}.get(color, "")
    try:
        r = requests.post("https://slack.com/api/chat.postMessage",
                          json={"channel": channel, "text": f"{icon} {message}",
                                "attachments": [{"color": color, "text": message,
                                                 "footer": "CCL Exporter"}]},
                          headers=headers, timeout=10)
        print("[slack] Sent." if r.json().get("ok") else f"[slack] Error: {r.json().get('error')}")
    except Exception as e:
        print(f"[slack] Failed: {e}")


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def login(page) -> bool:
    print("[login] Opening login page ...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

    # Poll: click Cloudflare "I am human" checkbox if it appears, else wait for login form
    print("[login] Waiting for login form (up to 60s, Cloudflare may appear) ...")
    for attempt in range(60):
        if await page.locator('input[placeholder="Type your login ID"]').count() > 0:
            print(f"[login] Login form ready after {attempt}s")
            break
        for cf_sel in [
            'iframe[src*="challenges.cloudflare"]',
            'iframe[title*="challenge" i]',
            'iframe[src*="turnstile"]',
            'iframe[src*="captcha"]',
        ]:
            try:
                cb = page.frame_locator(cf_sel).locator(
                    'input[type="checkbox"], .ctp-checkbox-label, #cf-stage'
                )
                if await cb.count() > 0:
                    await cb.first.click(timeout=3_000)
                    print(f"[cf] Cloudflare checkbox clicked (attempt {attempt})")
                    await page.wait_for_timeout(3_000)
                    break
            except Exception:
                pass
        await page.wait_for_timeout(1_000)
    else:
        await ss(page, "01_cf_timeout")
        raise RuntimeError("Login form never appeared — Cloudflare not cleared after 60s")

    await dismiss_cookie_banner(page)
    await ss(page, "01_login_page")

    # Notify: about to start login
    notify_slack_ccl("Soon you will Received Approval")

    # Step 1: Login ID -> Next
    await page.locator('input[placeholder="Type your login ID"]').fill(USERNAME)
    await page.locator('button:has-text("Next")').click()
    await page.wait_for_timeout(2_500)
    await ss(page, "02_after_next")
    print(f"[login] URL: {page.url}")

    # Step 2: Password -> Login
    await page.locator('input[type="password"]').fill(PASSWORD)
    await page.locator('button:has-text("Login")').click()
    await page.wait_for_timeout(3_000)
    await ss(page, "03_after_password")
    print(f"[login] URL: {page.url}")

    # Step 3: Authy 2FA push
    # Send "Please Approve" every 1 minute, check approval every 30s, loop 5 times.
    if "two_step" in page.url:
        print("[2fa] On 2FA page — starting approval loop (5 × 1 min) ...")
        await ss(page, "04_two_step")

        approved = False
        for attempt in range(1, 6):         # 5 iterations = 5 minutes max
            notify_slack_ccl("Please Approve")
            print(f"[2fa] 'Please Approve' sent (attempt {attempt}/5)")

            # Check every 30s — 2 checks per 1-minute iteration
            for _ in range(2):
                await page.wait_for_timeout(30_000)
                if "two_step" not in page.url:
                    approved = True
                    break

            if approved:
                break

        if approved:
            notify_slack_ccl("thankyou")
            print("[2fa] Approved!")
        else:
            notify_slack_ccl("Please upload manually")
            print("[2fa] Not approved after 5 attempts")
            return False

    try:
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except PwTimeout:
        pass
    await page.wait_for_timeout(3_000)
    await dismiss_cookie_banner(page)
    await ss(page, "05_dashboard")
    print(f"[login] Done. URL: {page.url}")
    return True


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
async def export_transactions(page) -> Path:
    print(f"\n[export] Starting transactions export {START_DATE} -> {END_DATE}")

    # --- 1. Click Reports in sidebar ---
    print("[nav] Clicking Reports ...")
    await dismiss_cookie_banner(page)
    await page.locator('a:has-text("Reports"), nav a:has-text("Reports")').first.click()
    await page.wait_for_timeout(2_000)
    await dismiss_cookie_banner(page)
    await ss(page, "10_reports_page")
    print(f"[nav] URL: {page.url}")

    # --- 2. Click Transactions Report ---
    print("[nav] Clicking Transactions Report ...")
    tx_link = page.locator(
        'a:has-text("Transactions Report"), '
        'a:has-text("Transaction Report"), '
        'li:has-text("Transactions Report"), '
        'td:has-text("Transactions Report")'
    )
    if await tx_link.count() > 0:
        await tx_link.first.click()
        await page.wait_for_timeout(2_000)
    else:
        await ss(page, "10b_no_tx_report")
        raise RuntimeError("Transactions Report link not found — check ccl_dbg_10b_no_tx_report.png")

    await dismiss_cookie_banner(page)
    await ss(page, "11_transactions_page")
    print(f"[nav] URL: {page.url}")

    # --- 3. Set Filter by = Created at (native <select>) ---
    print("[filter] Setting Filter by = Created at ...")
    filter_sel = page.locator('select').filter(has=page.locator('option[value="completed_at"]'))
    if await filter_sel.count() > 0:
        await filter_sel.first.select_option(value="created_at")
        await page.wait_for_timeout(500)
        print("[filter] Filter by = Created at")
    await ss(page, "12_filter_by_set")

    # --- 4. Pick dates using VueDatePicker calendar ---
    # Input type has no explicit type attr (dp__input class) — interact via calendar popup.
    print(f"[filter] Picking dates: {START_DATE} -> {END_DATE}")

    async def pick_calendar_date(input_idx: int, day: int):
        await page.locator('.dp__input').nth(input_idx).click()
        await page.wait_for_timeout(1_000)
        # Click the correct day in the current month (skip dp__cell_offset = other-month days)
        day_cells = page.locator('.dp__calendar_item:not(.dp__cell_offset) .dp__cell_inner')
        n = await day_cells.count()
        for i in range(n):
            if (await day_cells.nth(i).text_content() or "").strip() == str(day):
                await day_cells.nth(i).click()
                print(f"[filter] Picked day {day}")
                break
        await page.wait_for_timeout(500)
        # Confirm with Select button
        select_btn = page.locator('button:has-text("Select"), .dp__action_select')
        if await select_btn.count() > 0:
            await select_btn.first.click()
        await page.wait_for_timeout(600)

    await pick_calendar_date(0, START_DT.day)
    await pick_calendar_date(1, END_DT.day)
    await ss(page, "13_dates_filled")

    # --- 5. Open Advanced filter ---
    print("[filter] Opening Advanced filter ...")
    for sel in ['a:has-text("Advanced")', 'span:has-text("Advanced")',
                '*:has-text("Advanced"):not(title):not(script)']:
        adv = page.locator(sel)
        if await adv.count() > 0:
            await adv.last.click()
            await page.wait_for_timeout(1_500)
            print(f"[filter] Advanced opened via: {sel}")
            break
    await dismiss_cookie_banner(page)
    await ss(page, "14_advanced_open")

    # --- 6. Status: click field -> select Deleted -> close dropdown ---
    # Default: "All but Deleted". Click the field, then click "Deleted" in the
    # dropdown (white = excluded) to include it — Status becomes ALL.
    print("[filter] Fixing Status: including Deleted ...")

    for sel in ['text="All but"', '.multiselect:has-text("Deleted")',
                '.multiselect__tags:has-text("Deleted")']:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=3_000)
                print(f"[filter] Status dropdown opened via: {sel}")
                await page.wait_for_timeout(800)
                break
        except Exception:
            pass

    # Deleted option has class "option" (NOT "option selected")
    for sel in ['div.option:not(.selected):has-text("Deleted")',
                'div.option:has-text("Deleted")', '.option:has-text("Deleted")']:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(timeout=3_000)
                print(f"[filter] Deleted selected via: {sel}")
                await page.wait_for_timeout(500)
                break
        except Exception:
            pass

    # Close the dropdown with a safe body click (avoids navigating away)
    await page.evaluate("() => document.body.click()")
    await page.wait_for_timeout(600)
    await dismiss_cookie_banner(page)
    await ss(page, "15_status_all")

    # --- 7. Download CSV ---
    print("[export] Clicking Download CSV ...")
    download_btn = page.locator(
        'input[value="Download CSV"], '
        'input[value*="Download" i], '
        'button:has-text("Download CSV"), '
        'a:has-text("Download CSV")'
    )

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    dest = DOWNLOAD_DIR / FILENAME

    if await download_btn.count() > 0:
        try:
            async with page.expect_download(timeout=60_000) as dl_info:
                await download_btn.first.click()
            download = await dl_info.value
            await download.save_as(str(dest))
            print(f"[export] Saved -> {dest.resolve()}")
            await ss(page, "16_after_download")
            return dest
        except PwTimeout:
            await ss(page, "16_download_timeout")
            raise RuntimeError("Download timed out after 60s")
    else:
        await ss(page, "16_no_download_btn")
        raise RuntimeError("Download CSV button not found — check ccl_dbg_16_no_download_btn.png")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    print("=" * 60)
    print(f"[*] CCL Transactions Export")
    print(f"[*] Period  : {START_DATE} -> {END_DATE}")
    print(f"[*] File    : {FILENAME}")
    print(f"[*] S3      : {S3_ENABLED}")
    print("=" * 60)

    dest = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=IS_CI,
            slow_mo=0 if IS_CI else 80,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1440, "height": 900},
            user_agent=USER_AGENT,
            locale="en-GB",
            timezone_id="Europe/London",
        )
        await context.add_init_script(STEALTH_SCRIPT)
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)   # patches fingerprint signals → Cloudflare rarely triggers

        try:
            ok = await login(page)
            if not ok:
                raise RuntimeError("Login failed (2FA not approved in time)")
            dest = await export_transactions(page)

        except Exception as exc:
            msg = f"CCL export FAILED\nPeriod: {START_DATE} -> {END_DATE}\nError: {exc}"
            print(f"\n[!] {msg}")
            notify_slack(msg, color="danger")
            try:
                await ss(page, "error_final")
            except Exception:
                pass
            raise
        finally:
            if not IS_CI:
                try:
                    input("Press Enter to close browser ...")
                except EOFError:
                    pass
            await browser.close()

    # --- S3 upload ---
    s3_uri = None
    if S3_ENABLED and dest and dest.exists():
        try:
            s3_uri = upload_to_s3(dest)
        except Exception as e:
            notify_slack(f"CCL S3 upload FAILED\nError: {e}", color="warning")

    # --- Slack success notification ---
    if dest and dest.exists():
        size_kb = dest.stat().st_size // 1024
        lines = [
            "*CCL Transactions Export Complete*",
            f"Period : `{START_DATE}` -> `{END_DATE}`",
            f"File   : `{dest.name}` ({size_kb} KB)",
        ]
        if s3_uri:
            lines.append(f"S3     : `{s3_uri}`")
        notify_slack("\n".join(lines), color="good")

    print(f"\n[+] Done! -> {dest.resolve() if dest else 'no file'}")


if __name__ == "__main__":
    asyncio.run(main())
