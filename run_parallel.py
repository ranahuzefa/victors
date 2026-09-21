"""
run_parallel.py -- simple parallel batch processor (no frills).
For the full-featured version with progress bar and live controls, use runner.py.
"""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright
from anti_detect import STEALTH_ARGS, STEALTH_SCRIPT, launch_context_options, random_viewport
from av_selectors import S
from auth import is_logged_in, try_credential_login
from core import (
    process_one, OUTPUT_FORMAT, SUPPORTED_EXT, HEADLESS, SLOW_MO_MS,
    PROFILE_REFRESH_MARKER,
)

load_dotenv()
INPUT_DIR = Path(os.environ["INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["OUTPUT_DIR"])
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 2))
PROFILE_DIR = OUTPUT_DIR / ".browser_profile"
AUTO_REFRESH_PROFILE = os.environ.get("AUTO_REFRESH_PROFILE_ON_ACCOUNT_REDIRECT", "true").lower() == "true"
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chromium").strip() or None
BROWSER_USER_AGENT = os.environ.get(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.7680.178 Safari/537.36",
).strip() or None


def refresh_browser_profile_if_requested():
    if not AUTO_REFRESH_PROFILE or not PROFILE_REFRESH_MARKER.exists():
        return False

    reason = "account redirect"
    try:
        marker = json.loads(PROFILE_REFRESH_MARKER.read_text(encoding="utf-8"))
        reason = marker.get("reason") or reason
    except Exception:
        pass

    stamp = time.strftime("%Y%m%d_%H%M%S")
    archived = OUTPUT_DIR / f".browser_profile.archived_{stamp}"
    if PROFILE_DIR.exists():
        if archived.exists():
            archived = OUTPUT_DIR / f".browser_profile.archived_{stamp}_{os.getpid()}"
        shutil.move(str(PROFILE_DIR), str(archived))
        print(f"[login] Archived browser profile for refresh: {archived.name}")

    try:
        PROFILE_REFRESH_MARKER.unlink()
    except FileNotFoundError:
        pass

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[login] Fresh browser profile created after: {reason}")
    return True


def login_sync(profile_dir):
    """Run login ONCE using sync Playwright. Saves cookies to profile_dir."""
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=HEADLESS,
            channel=BROWSER_CHANNEL if HEADLESS else None,
            user_agent=BROWSER_USER_AGENT if HEADLESS else None,
            accept_downloads=True,
            viewport=random_viewport(),
            args=STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
            **launch_context_options(),
        )
        context.add_init_script(STEALTH_SCRIPT)
        page = context.new_page()
        page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        if is_logged_in(page):
            print("[login] Already logged in (profile saved).")
        else:
            print("[login] Attempting credential login...")
            ok, reason = try_credential_login(page)
            if not ok:
                context.close()
                raise RuntimeError(f"Automated login failed: {reason}")
            print("[login] Automated login confirmed.")
        context.close()


def log(*args):
    print("[vectorizer]", *args, flush=True)


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in INPUT_DIR.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXT)
    if not files:
        log("No image files found in INPUT_DIR")
        return

    log(f"Found {len(files)} input file(s) in {INPUT_DIR}")
    log(f"Max concurrent tabs: {MAX_CONCURRENT}")

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            channel=BROWSER_CHANNEL if HEADLESS else None,
            user_agent=BROWSER_USER_AGENT if HEADLESS else None,
            slow_mo=SLOW_MO_MS,
            accept_downloads=True,
            viewport=random_viewport(),
            args=STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
            **launch_context_options(),
        )
        await context.add_init_script(STEALTH_SCRIPT)

        tasks = [process_one(context, sem, fp, i + 1, len(files)) for i, fp in enumerate(files)]
        results = await asyncio.gather(*tasks)

        await context.close()

    ok = sum(1 for r in results if r["ok"])
    fail = len(results) - ok
    log(f"Done. Success: {ok}, Failed: {fail}")


if __name__ == "__main__":
    OUTPUT_DIR = Path(os.environ["OUTPUT_DIR"])
    PROFILE_DIR = OUTPUT_DIR / ".browser_profile"
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    refresh_browser_profile_if_requested()
    login_sync(PROFILE_DIR)
    asyncio.run(main())
