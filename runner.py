"""
runner.py -- intelligent headless batch processor.
Compact single-line live progress synced from real DOM bars.
Commands: p=pause r=resume s=N=skip c=N=concurrent q=status stop=quit
"""

import asyncio
import json
import os
import sys
import time
import traceback
import threading
import shutil
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright
from anti_detect import (
    STEALTH_ARGS,
    STEALTH_SCRIPT,
    launch_context_options,
    random_viewport,
    stealth_http_headers,
)
from av_selectors import S
from auth import is_logged_in_sync, try_credential_login
from core import (
    process_one, OUTPUT_FORMAT, SUPPORTED_EXT, HEADLESS, SLOW_MO_MS,
    STALL_WARN_SECONDS, PROFILE_REFRESH_MARKER, profile_refresh_requested,
)
from core import OUTPUT_DIR as _CORE_OUTPUT_DIR

load_dotenv()
INPUT_DIR = Path(os.environ["INPUT_DIR"])
OUTPUT_DIR = Path(os.environ["OUTPUT_DIR"])
MAX_SAFE_CONCURRENT = max(1, int(os.environ.get("MAX_SAFE_CONCURRENT", 2)))
MAX_CONCURRENT = min(
    max(1, int(os.environ.get("MAX_CONCURRENT", 2))),
    MAX_SAFE_CONCURRENT,
)
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chromium").strip() or None
BROWSER_USER_AGENT = os.environ.get(
    "BROWSER_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.7680.178 Safari/537.36",
)
AUTO_LOGIN = os.environ.get("AUTO_LOGIN", "true").lower() == "true"
MANUAL_LOGIN_FALLBACK = os.environ.get("MANUAL_LOGIN_FALLBACK", "true").lower() == "true"
PROFILE_DIR = OUTPUT_DIR / ".browser_profile"
PROGRESS_FILE = OUTPUT_DIR / "progress.json"
AUTO_REFRESH_PROFILE = os.environ.get("AUTO_REFRESH_PROFILE_ON_ACCOUNT_REDIRECT", "true").lower() == "true"

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── state ───────────────────────────────────────────────────
state = {
    "done": [],
    "failed": [],
    "skipped": set(),
    "active": {},       # name -> {"size": int, "progress": float}
    "total": 0,
    "start_time": 0,
}
stats_lock = threading.Lock()
cmd_queue = None
paused = False
stop_requested = False
loop = None
console_lock = threading.Lock()
progress_lines = 0
dashboard_running = False


def fmt_time(sec):
    if sec < 60: return f"{sec:.0f}s"
    m, s = divmod(int(sec), 60)
    if m < 60: return f"{m}m{s}s"
    h, m = divmod(m, 60); return f"{h}h{m}m"

def fmt_size(b):
    if b < 1024: return f"{b}B"
    if b < 1024*1024: return f"{b/1024:.0f}KB"
    return f"{b/(1024*1024):.1f}MB"

def short_name(name, n=24):
    return name if len(name) <= n else name[:n-3] + "..."

def on_done_cb(fp, out, elapsed, sz):
    with stats_lock:
        state["done"].append({"n": fp.name, "sz": sz, "t": elapsed})
        state["active"].pop(fp.name, None)
    save()

def on_fail_cb(fp, err, elapsed):
    with stats_lock:
        state["failed"].append({"n": fp.name, "e": err, "t": elapsed})
        state["active"].pop(fp.name, None)
    save()

def on_progress(fp_name):
    def cb(bars):
        with stats_lock:
            if fp_name in state["active"]:
                state["active"][fp_name]["bars"] = dict(bars)
    return cb

def on_status(fp_name):
    def cb(status, attempt, **extra):
        with stats_lock:
            if fp_name in state["active"]:
                state["active"][fp_name].update({
                    "status": status,
                    "attempt": attempt,
                    **extra,
                })
    return cb

def on_start_cb(fp, size):
    with stats_lock:
        state["active"][fp.name] = {
            "size": size,
            "bars": {},
            "status": "starting",
            "attempt": 1,
            "stall_seconds": 0,
        }

def save():
    try:
        with stats_lock:
            d = {"done": state["done"], "failed": state["failed"],
                 "active": state["active"],
                 "total": state["total"],
                 "elapsed": time.monotonic() - state["start_time"] if state["start_time"] else 0}
        with open(PROGRESS_FILE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


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
        print(f"[runner] Archived browser profile for refresh: {archived.name}")

    try:
        PROFILE_REFRESH_MARKER.unlink()
    except FileNotFoundError:
        pass

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[runner] Fresh browser profile created after: {reason}")
    return True


def _image_pct(bars):
    return min(100, max(0,
        bars.get("upload", 0) * 0.15
        + bars.get("process", 0) * 0.70
        + bars.get("fetch", 0) * 0.15
    ))


def _bar(pct, width):
    filled = int(width * pct / 100)
    return "#" * filled + "-" * (width - filled)


def render_progress():
    """Build a fixed dashboard with active images and overall progress."""
    with stats_lock:
        dn = len(state["done"]); fn = len(state["failed"])
        tot = state["total"]; comp = dn + fn
        elapsed = time.monotonic() - state["start_time"] if state["start_time"] else 0
        active = dict(state["active"])

    # Every image contributes equally. Active images use all three stages so
    # the batch bar cannot reach 100% before every image is complete.
    progress_sum = comp * 100
    for info in active.values():
        bars = info.get("bars", {})
        progress_sum += min(99, _image_pct(bars))

    agg_pct = progress_sum / tot if tot > 0 else 0
    agg_pct = min(100, max(0, agg_pct))

    def stage_label(bars):
        u = bars.get("upload", 0); p = bars.get("process", 0); f2 = bars.get("fetch", 0)
        if u < 100:
            return f"Up {u:.0f}%"
        if p < 100:
            return f"Proc {p:.0f}%"
        if f2 < 100:
            return f"Fetch {f2:.0f}%"
        return "Done"

    eta_str = ""
    if dn >= 2:
        done_mb = sum(d["sz"] for d in state["done"]) / (1024*1024)
        done_sec = sum(d["t"] for d in state["done"])
        if done_mb > 0.01:
            rate = done_sec / done_mb
            remain_mb = 0
            for name, info in active.items():
                if info.get("bars", {}).get("fetch", 0) < 100:
                    remain_mb += info.get("size", 0) / (1024*1024)
            if remain_mb > 0:
                eta_sec = remain_mb * rate / max(1, len(active))
                eta_str = f" ETA:{fmt_time(eta_sec)}"

    terminal = shutil.get_terminal_size(fallback=(120, 30))
    # Never exceed the real terminal width. Wrapped dashboard rows break the
    # cursor-up redraw logic and leave stale progress bars behind.
    width = max(40, terminal.columns - 1)
    bar_width = max(12, min(30, width - 48))
    name_width = max(14, width - bar_width - 24)
    max_active_rows = max(1, terminal.lines - 8)

    lines = []
    active_items = sorted(active.items())
    for name, info in active_items[:max_active_rows]:
        bars = info.get("bars", {})
        pct = _image_pct(bars)
        label = short_name(name, name_width).ljust(name_width)
        status = info.get("status", "starting")
        attempt = info.get("attempt", 1)
        stalled = info.get("stall_seconds", 0)
        if status == "retrying":
            stage = f"Retry {attempt}"
        elif stalled >= STALL_WARN_SECONDS:
            stage = f"STUCK {stalled}s"
        elif status in ("opening", "uploading", "downloading"):
            stage = status.title()
        else:
            stage = stage_label(bars)
        attempt_suffix = f" A{attempt}" if attempt > 1 else ""
        lines.append(
            f"{label} [{_bar(pct, bar_width)}] {stage:>10} {pct:5.1f}%{attempt_suffix}"
        )
    hidden = len(active_items) - max_active_rows
    if hidden > 0:
        lines.append(f"... {hidden} more active images")

    overall = (
        f"OVERALL {comp}/{tot} [{_bar(agg_pct, bar_width)}] "
        f"{agg_pct:5.1f}% | {fmt_time(elapsed)}{eta_str}"
    )
    lines.append(overall[:width])
    return "\n".join(line[:width] for line in lines)


def _clear_progress_locked():
    global progress_lines
    for index in range(progress_lines):
        sys.stdout.write("\r\033[K")
        if index < progress_lines - 1:
            sys.stdout.write("\033[1A")
    progress_lines = 0


def write_progress(text):
    """Redraw the fixed multi-line dashboard."""
    global progress_lines, dashboard_running
    with console_lock:
        _clear_progress_locked()
        lines = text.splitlines() or [""]
        sys.stdout.write("\n".join(lines))
        sys.stdout.flush()
        progress_lines = len(lines)
        dashboard_running = True


def clear_progress():
    global dashboard_running
    with console_lock:
        _clear_progress_locked()
        sys.stdout.flush()
        dashboard_running = False


# ── stdin reader ──────────────────────────────────────────────
def stdin_reader():
    global paused, stop_requested
    print("[runner] p=pause r=resume s=N=skip c=N=concurrent q=status stop=quit\n")
    while True:
        try:
            line = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if not line: continue
        parts = line.split()
        c = parts[0]
        if c in ("p", "pause"):
            paused = True; print("\n[runner] PAUSED. r to resume.")
        elif c in ("r", "resume"):
            paused = False; print("\n[runner] RESUMED.")
        elif c in ("s", "skip") and len(parts) > 1:
            try:
                state["skipped"].add(int(parts[1]) - 1)
                print(f"\n[runner] Skip #{parts[1]}")
            except ValueError: print("[runner] s <number>")
        elif c in ("c", "concurrent") and len(parts) > 1:
            try:
                n = int(parts[1])
                if 1 <= n <= 20:
                    global MAX_CONCURRENT; MAX_CONCURRENT = n
                    print(f"\n[runner] Concurrent={n}")
                else: print("[runner] 1-20 only")
            except ValueError: print("[runner] c <number>")
        elif c in ("q", "status"):
            print("\n" + render_progress() + "\n")
        elif c in ("h", "help"):
            print("p/r pause|resume  s N skip#  c N concurrent#  q status  stop quit")
        elif c == "stop":
            stop_requested = True; print("\n[runner] Stopping..."); break
        else:
            print(f"[runner] ? {line}  (h=help)")


# ── notifications ────────────────────────────────────────────
def notify_desktop(title, msg):
    try:
        from plyer import notification
        notification.notify(title=title, message=msg, timeout=10)
    except Exception: pass

async def notify_discord(msg):
    if not DISCORD_WEBHOOK: return
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            await s.post(DISCORD_WEBHOOK, json={"content": msg})
    except Exception: pass

async def notify_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        import aiohttp
        u = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as s:
            await s.post(u, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})
    except Exception: pass

async def send_done(elapsed, ok, fail):
    m = f"Vectorizer batch done!\nOK:{ok} Fail:{fail}\nTime:{fmt_time(elapsed)}\nOutput:{OUTPUT_DIR}"
    notify_desktop("Vectorizer.ai Done", m)
    await notify_discord(m)
    await notify_telegram(m)


# ── login ────────────────────────────────────────────────────
def _open_login_browser(playwright, profile_dir, headless):
    vp = random_viewport()
    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        channel=BROWSER_CHANNEL if headless else None,
        user_agent=BROWSER_USER_AGENT if headless else None,
        accept_downloads=True,
        viewport=vp,
        args=STEALTH_ARGS,
        ignore_default_args=["--enable-automation"],
        **launch_context_options(),
    )
    # Inject anti-detection before any pages open
    ctx.add_init_script(STEALTH_SCRIPT)
    return ctx


def login(profile_dir):
    with sync_playwright() as p:
        # Session validation and credential login both work headlessly.
        ctx = _open_login_browser(p, profile_dir, headless=HEADLESS)
        pg = ctx.new_page()
        pg.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
        pg.wait_for_timeout(3000)
        ok, detail = is_logged_in_sync(pg)
        if ok:
            print(f"[login] Already logged in. ({detail})")
        elif AUTO_LOGIN:
            print(f"[login] Not logged in: {detail}")
            print("[login] Attempting credential login...")
            ok, reason = try_credential_login(pg)
            if ok:
                print(f"[login] Automated login confirmed. ({reason})")
            else:
                print(f"[login] Automated login failed: {reason}")

        if not ok and MANUAL_LOGIN_FALLBACK:
            if HEADLESS:
                ctx.close()
                print("[login] Opening a browser for manual fallback.")
                ctx = _open_login_browser(p, profile_dir, headless=False)
                pg = ctx.new_page()
                pg.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
                pg.wait_for_timeout(2000)
            print("\n" + "=" * 50)
            print(" LOGIN REQUIRED: Log in manually in the browser.")
            print(' Click "Log In" and sign in, then press ENTER.')
            print("=" * 50)
            input(">>> Press ENTER: ")
            pg.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
            pg.wait_for_timeout(4000)
            ok, detail2 = is_logged_in_sync(pg)
            print(f"[login] Confirmed: {detail2}" if ok else f"[login] Proceeding (login may be incomplete): {detail2}")
        elif not ok:
            ctx.close()
            raise RuntimeError(
                "Login failed and MANUAL_LOGIN_FALLBACK=false. Check credentials, "
                "CAPTCHA/2FA, or the failed SSO selectors."
            )
        ctx.close()


# ── main ─────────────────────────────────────────────────────
async def main():
    global cmd_queue, loop, paused, stop_requested, state
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # ── setup log file ─────────────────────────────────────────
    run_id = time.strftime("%Y%m%d_%H%M%S")
    LOG_FILE = OUTPUT_DIR / f"run_{run_id}.log"
    JSON_LOG_FILE = OUTPUT_DIR / f"run_{run_id}.jsonl"
    _log_fp = open(LOG_FILE, "w", encoding="utf-8")
    _json_log_fp = open(JSON_LOG_FILE, "w", encoding="utf-8")

    import builtins
    _orig_print = builtins.print
    def tee_print(*args, **kwargs):
        import datetime
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        msg = " ".join(str(a) for a in args)
        line = f"[{ts}] {msg}"
        # Vectorizer diagnostics belong in the log files. Printing them to the
        # terminal clears and redraws all progress rows, causing flicker and
        # broken cursor positioning.
        file_only = msg.startswith("[vectorizer]")
        if not file_only:
            with console_lock:
                _clear_progress_locked()
                _orig_print(line, **kwargs)
        try:
            _log_fp.write(line + "\n")
            _log_fp.flush()
            _json_log_fp.write(json.dumps({
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "message": msg,
            }) + "\n")
            _json_log_fp.flush()
        except Exception:
            pass
    builtins.print = tee_print  # capture ALL output

    def close_logs():
        builtins.print = _orig_print
        _log_fp.close()
        _json_log_fp.close()
        _orig_print(f"[runner] Logs saved: {LOG_FILE} | {JSON_LOG_FILE}")

    print(f"[runner] Log: {LOG_FILE}")

    files = sorted(p for p in INPUT_DIR.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_EXT)
    if not files:
        print("[runner] No images found.")
        close_logs()
        return

    skip_existing = "--force" not in sys.argv
    to_process = []
    for fp in files:
        exp = OUTPUT_DIR / f"{fp.stem}.{OUTPUT_FORMAT.lower()}"
        if skip_existing and exp.exists() and exp.stat().st_size > 100:
            print(f"[runner] skip (exists): {fp.name}"); continue
        to_process.append(fp)

    if not to_process:
        print("[runner] All done. Use --force to redo.")
        close_logs()
        return

    state["total"] = len(to_process)
    state["start_time"] = time.monotonic()

    print(f"[runner] {len(to_process)} files | concurrent={MAX_CONCURRENT}\n")

    cmd_queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    threading.Thread(target=stdin_reader, daemon=True).start()

    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async with async_playwright() as p:
        vp = random_viewport()
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=HEADLESS, slow_mo=SLOW_MO_MS,
            channel=BROWSER_CHANNEL if HEADLESS else None,
            user_agent=BROWSER_USER_AGENT if HEADLESS else None,
            accept_downloads=True, viewport=vp,
            args=STEALTH_ARGS,
            ignore_default_args=["--enable-automation"],
            **launch_context_options(),
        )
        # Inject anti-detection into every page in this context
        await ctx.add_init_script(STEALTH_SCRIPT)

        tasks = []
        for i, fp in enumerate(to_process):
            if i in state["skipped"]: continue

            async def wrapper(ct, fpath, idx, tot):
                global paused, stop_requested
                while paused and not stop_requested:
                    await asyncio.sleep(0.5)
                if stop_requested:
                    return {"ok": False, "error": "stopped", "elapsed": 0}
                return await process_one(
                    ct, sem, fpath, idx, tot,
                    on_done_cb, on_fail_cb, on_progress(fpath.name), on_start_cb,
                    on_status(fpath.name))

            tasks.append(wrapper(ctx, fp, i + 1, len(to_process)))

        # Progress printer
        async def printer():
            last_snapshot = 0
            while True:
                write_progress(render_progress())
                now = time.monotonic()
                if now - last_snapshot >= 10:
                    with stats_lock:
                        snapshot = {
                            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "event": "progress_snapshot",
                            "done": len(state["done"]),
                            "failed": len(state["failed"]),
                            "total": state["total"],
                            "active": state["active"],
                        }
                    _json_log_fp.write(json.dumps(snapshot) + "\n")
                    _json_log_fp.flush()
                    save()
                    last_snapshot = now
                await asyncio.sleep(2)
        ptask = asyncio.create_task(printer())
        results = await asyncio.gather(*tasks)
        ptask.cancel()
        try: await ptask
        except asyncio.CancelledError: pass
        clear_progress()
        await ctx.close()

    if profile_refresh_requested():
        print("[runner] Account/pricing redirect detected; browser profile refresh requested.")
        print("[runner] Stop this batch now. The next start will archive the old profile and login with a fresh profile.")
        close_logs()
        raise SystemExit(3)

    elapsed = time.monotonic() - state["start_time"]
    ok_n = sum(1 for r in results if r.get("ok"))
    fail_n = len(results) - ok_n
    skip_n = len(files) - len(to_process)

    print("\n" + "-" * 50)
    print(f" DONE  {fmt_time(elapsed)} | OK:{ok_n} FAIL:{fail_n}" + (f" SKIP:{skip_n}" if skip_n else ""))
    if state["done"]:
        total_sz = sum(d["sz"] for d in state["done"])
        print(f" Data: {fmt_size(total_sz)} in {fmt_time(elapsed)}")
        avg = sum(d["t"] for d in state["done"]) / len(state["done"])
        print(f" Avg/file: {fmt_time(avg)}")
    print("-" * 50)
    await send_done(elapsed, ok_n, fail_n)
    close_logs()


if __name__ == "__main__":
    print(f"[runner] Login check (headless={HEADLESS})...")
    refresh_browser_profile_if_requested()
    login(PROFILE_DIR)
    asyncio.run(main())
