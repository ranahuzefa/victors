"""Persistent AutoVector browser worker.

Reads JSON commands on stdin and keeps a Playwright browser context alive
between batches. The bridge sends:
  {"cmd":"run","id":"...","files":["image.png"]}
  {"cmd":"stop"}
  {"cmd":"shutdown"}
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from anti_detect import STEALTH_ARGS, STEALTH_SCRIPT, launch_context_options, random_viewport
from auth import is_logged_in_sync, try_credential_login
from core import (
    OUTPUT_DIR,
    SUPPORTED_EXT,
    HEADLESS,
    PROFILE_REFRESH_MARKER,
    process_one,
    profile_refresh_requested,
)


load_dotenv()

INPUT_DIR = Path(os.environ["INPUT_DIR"])
PROFILE_DIR = OUTPUT_DIR / ".browser_profile"
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chromium").strip() or None
BROWSER_USER_AGENT = os.environ.get(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.7680.178 Safari/537.36",
)
AUTO_LOGIN = os.environ.get("AUTO_LOGIN", "true").lower() == "true"
MANUAL_LOGIN_FALLBACK = os.environ.get("MANUAL_LOGIN_FALLBACK", "true").lower() == "true"
AUTO_REFRESH_PROFILE = os.environ.get("AUTO_REFRESH_PROFILE_ON_ACCOUNT_REDIRECT", "true").lower() == "true"


def emit(event: dict) -> None:
    print(json.dumps(event, separators=(",", ":")), flush=True)


def refresh_browser_profile_if_requested() -> bool:
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
        emit({"event": "worker-log", "level": "warn", "message": f"Archived browser profile: {archived.name}"})

    try:
        PROFILE_REFRESH_MARKER.unlink()
    except FileNotFoundError:
        pass

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    emit({"event": "worker-log", "level": "warn", "message": f"Fresh browser profile created after: {reason}"})
    return True


def login_sync() -> None:
    from playwright.sync_api import sync_playwright

    refresh_browser_profile_if_requested()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            channel=BROWSER_CHANNEL if HEADLESS else None,
            user_agent=BROWSER_USER_AGENT if HEADLESS else None,
            accept_downloads=True,
            viewport=random_viewport(),
            args=STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
            **launch_context_options(),
        )
        ctx.add_init_script(STEALTH_SCRIPT)
        page = ctx.new_page()
        page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        ok, detail = is_logged_in_sync(page)
        if ok:
            emit({"event": "worker-log", "level": "info", "message": f"Already logged in. ({detail})"})
        elif AUTO_LOGIN:
            emit({"event": "worker-log", "level": "info", "message": f"Not logged in: {detail}; attempting credential login"})
            ok, reason = try_credential_login(page)
            if ok:
                emit({"event": "worker-log", "level": "info", "message": f"Automated login confirmed. ({reason})"})
            else:
                emit({"event": "worker-log", "level": "warn", "message": f"Automated login failed: {reason}"})

        if not ok and MANUAL_LOGIN_FALLBACK:
            if HEADLESS:
                ctx.close()
                emit({"event": "worker-log", "level": "warn", "message": "Opening visible browser for manual fallback"})
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=str(PROFILE_DIR),
                    headless=False,
                    accept_downloads=True,
                    viewport=random_viewport(),
                    args=STEALTH_ARGS,
                    ignore_default_args=["--enable-automation"],
                    **launch_context_options(),
                )
                ctx.add_init_script(STEALTH_SCRIPT)
                page = ctx.new_page()
                page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
            input("LOGIN REQUIRED: complete login in the browser, then press ENTER: ")
            page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            ok, detail = is_logged_in_sync(page)
            emit({"event": "worker-log", "level": "info" if ok else "warn", "message": f"Manual login check: {detail}"})
        elif not ok:
            ctx.close()
            raise RuntimeError("Login failed and MANUAL_LOGIN_FALLBACK=false")
        ctx.close()


class BrowserWorker:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.stop_requested = False

    async def start(self) -> None:
        self.playwright = await async_playwright().start()
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            channel=BROWSER_CHANNEL if HEADLESS else None,
            user_agent=BROWSER_USER_AGENT if HEADLESS else None,
            accept_downloads=True,
            viewport=random_viewport(),
            args=STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
            **launch_context_options(),
        )
        await self.context.add_init_script(STEALTH_SCRIPT)
        page = await self.context.new_page()
        await page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
        await page.wait_for_timeout(1000)
        emit({"event": "worker-ready"})

    async def close(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def run_batch(self, command: dict) -> None:
        batch_id = command.get("id") or str(int(time.time()))
        names = [str(name) for name in command.get("files", [])]
        files = [
            INPUT_DIR / name
            for name in names
            if (INPUT_DIR / name).is_file() and (INPUT_DIR / name).suffix.lower() in SUPPORTED_EXT
        ]
        emit({"event": "worker-batch-start", "id": batch_id, "total": len(files)})

        active = {}
        done = []
        failed = []

        def snapshot() -> None:
            emit({
                "event": "progress_snapshot",
                "done": len(done),
                "failed": len(failed),
                "total": len(files),
                "active": active,
            })

        for index, file_path in enumerate(files, start=1):
            if self.stop_requested:
                failed.append({"n": file_path.name, "e": "stopped", "t": 0})
                break

            started = time.monotonic()

            def on_start(fp, size):
                active[fp.name] = {
                    "size": size,
                    "bars": {},
                    "status": "starting",
                    "attempt": 1,
                    "stall_seconds": 0,
                }
                snapshot()

            def on_progress(fp_name):
                def cb(bars):
                    if fp_name in active:
                        active[fp_name]["bars"] = dict(bars)
                    snapshot()
                return cb

            def on_status(fp_name):
                def cb(status, attempt, **extra):
                    if fp_name in active:
                        active[fp_name].update({"status": status, "attempt": attempt, **extra})
                    snapshot()
                return cb

            def on_done(fp, out, elapsed, size):
                active.pop(fp.name, None)
                done.append({"n": fp.name, "sz": size, "t": elapsed})
                emit({"event": "done", "file": fp.name, "output": str(out), "elapsed": elapsed})
                snapshot()

            def on_fail(fp, error, elapsed):
                active.pop(fp.name, None)
                failed.append({"n": fp.name, "e": error, "t": elapsed})
                emit({"event": "message", "message": f"[vectorizer] [{index}/{len(files)}] attempt failed: {error}"})
                snapshot()

            result = await process_one(
                self.context,
                asyncio.Semaphore(1),
                file_path,
                index,
                len(files),
                on_done,
                on_fail,
                on_progress(file_path.name),
                on_start,
                on_status(file_path.name),
            )
            if profile_refresh_requested():
                emit({"event": "worker-profile-refresh-requested"})
                break
            if not result.get("ok") and not any(item["n"] == file_path.name for item in failed):
                failed.append({"n": file_path.name, "e": result.get("error", "failed"), "t": time.monotonic() - started})
                snapshot()

        emit({"event": "worker-batch-done", "id": batch_id, "ok": len(done), "fail": len(failed)})


async def stdin_loop(worker: BrowserWorker) -> None:
    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if line == "":
            break
        try:
            command = json.loads(line)
        except Exception:
            emit({"event": "worker-log", "level": "warn", "message": f"Invalid command: {line.strip()}"})
            continue

        cmd = command.get("cmd")
        if cmd == "run":
            worker.stop_requested = False
            await worker.run_batch(command)
        elif cmd == "stop":
            worker.stop_requested = True
            emit({"event": "worker-stopping"})
        elif cmd == "shutdown":
            emit({"event": "worker-shutdown"})
            break
        else:
            emit({"event": "worker-log", "level": "warn", "message": f"Unknown command: {cmd}"})


async def main() -> None:
    worker = BrowserWorker()
    try:
        await worker.start()
        await stdin_loop(worker)
    finally:
        await worker.close()


if __name__ == "__main__":
    login_sync()
    asyncio.run(main())
