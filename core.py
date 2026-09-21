"""Shared configuration and resilient per-image processing."""

import asyncio
import json
import os
import random
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from anti_detect import apply_stealth_to_page
from av_selectors import S

load_dotenv()

OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "SVG").upper()
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", Path(__file__).resolve().parent / "output"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
SLOW_MO_MS = 0
PROFILE_REFRESH_MARKER = OUTPUT_DIR / ".profile_refresh_requested.json"

CONVERSION_TIMEOUT_MS = int(os.environ.get("CONVERSION_TIMEOUT_MS", 5 * 60_000))
DOWNLOAD_TIMEOUT_MS = int(os.environ.get("DOWNLOAD_TIMEOUT_MS", 120_000))
DOM_CALL_TIMEOUT_MS = int(os.environ.get("DOM_CALL_TIMEOUT_MS", 15_000))
STALL_WARN_SECONDS = int(os.environ.get("STALL_WARN_SECONDS", 30))
STALL_TIMEOUT_SECONDS = int(os.environ.get("STALL_TIMEOUT_SECONDS", 90))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", 2))
RETRY_DELAY_SECONDS = int(os.environ.get("RETRY_DELAY_SECONDS", 5))
RETRY_JITTER_SECONDS = float(os.environ.get("RETRY_JITTER_SECONDS", 2))
MIN_REQUEST_INTERVAL_SECONDS = float(os.environ.get("MIN_REQUEST_INTERVAL_SECONDS", 8))
RATE_LIMIT_COOLDOWN_SECONDS = int(os.environ.get("RATE_LIMIT_COOLDOWN_SECONDS", 300))
SERVICE_BLOCK_COOLDOWN_SECONDS = int(os.environ.get("SERVICE_BLOCK_COOLDOWN_SECONDS", 900))

SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp"}


class NonRetryableServiceError(RuntimeError):
    """A service response that should not trigger another attempt."""


class RateLimitError(NonRetryableServiceError):
    """The service asked the client to stop sending requests."""


class ServiceCircuit:
    """Coordinate request pacing and service-wide cooldowns across tasks."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._next_request_at = 0.0
        self._blocked_until = 0.0
        self._block_reason = ""

    async def wait_for_request_slot(self):
        async with self._lock:
            now = time.monotonic()
            if now < self._blocked_until:
                remaining = int(self._blocked_until - now) + 1
                raise NonRetryableServiceError(
                    f"service circuit open for {remaining}s: {self._block_reason}"
                )
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + MIN_REQUEST_INTERVAL_SECONDS
        if delay:
            await asyncio.sleep(delay)

    async def block(self, reason, cooldown_seconds):
        async with self._lock:
            self._block_reason = reason
            self._blocked_until = max(
                self._blocked_until, time.monotonic() + cooldown_seconds
            )


SERVICE_CIRCUIT = ServiceCircuit()
PAGE_POOL = []
PAGE_POOL_LOCK = asyncio.Lock()


def log(*args):
    print("[vectorizer]", *args, flush=True)


def profile_refresh_requested():
    return PROFILE_REFRESH_MARKER.exists()


def request_profile_refresh(reason, url=None):
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        PROFILE_REFRESH_MARKER.write_text(
            json.dumps(
                {
                    "reason": reason,
                    "url": url,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def is_account_redirect_url(current_url):
    lowered = current_url.lower()
    return any(
        marker in lowered
        for marker in (
            "/pricing",
            "/price",
            "/plan",
            "/plans",
            "/billing",
            "/subscribe",
            "/subscription",
            "/login",
            "/signin",
        )
    )


async def _get_work_page(context):
    async with PAGE_POOL_LOCK:
        while PAGE_POOL:
            page = PAGE_POOL.pop()
            if not page.is_closed():
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                return page

    page = await context.new_page()
    await apply_stealth_to_page(page)
    page.set_default_timeout(20_000)
    return page


async def _close_work_page(page):
    try:
        if page and not page.is_closed():
            await page.close()
    except Exception:
        pass


async def _return_to_home(page):
    if not page or page.is_closed():
        return False

    try:
        await page.bring_to_front()
    except Exception:
        pass

    try:
        current_url = page.url
        if "vectorizer.ai" not in current_url:
            await page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
            await page.wait_for_timeout(900)
            return True
    except Exception:
        pass

    try:
        home = page.locator(S.HOME_LINK).first
        if await home.is_visible(timeout=2500):
            await home.click(timeout=5000)
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
            await page.wait_for_timeout(900)
            return True
    except Exception:
        pass

    try:
        await page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
        await page.wait_for_timeout(900)
        return True
    except Exception:
        return False


async def _release_work_page(page):
    if not page or page.is_closed():
        return
    if not await _return_to_home(page):
        await _close_work_page(page)
        return
    async with PAGE_POOL_LOCK:
        PAGE_POOL.append(page)


async def _save_diagnostics(page, file_path, attempt, error, bars):
    prefix = OUTPUT_DIR / f"fail_{file_path.stem}_attempt{attempt}"
    try:
        await asyncio.wait_for(
            page.screenshot(path=str(prefix) + ".png", full_page=True),
            timeout=DOM_CALL_TIMEOUT_MS / 1000,
        )
    except Exception:
        pass

    diagnostic = {
        "file": str(file_path),
        "attempt": attempt,
        "error_type": type(error).__name__,
        "error": str(error),
        "url": page.url,
        "progress": bars,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        Path(str(prefix) + ".json").write_text(
            json.dumps(diagnostic, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


async def _process_attempt(context, file_path, idx, total, attempt,
                           on_progress=None, on_status=None, page=None):
    file_size = file_path.stat().st_size
    tag = f"[{idx}/{total} attempt {attempt}/{MAX_RETRIES + 1}]"
    page = page or await _get_work_page(context)
    page.set_default_timeout(20_000)
    last_bars = {}
    service_responses = []

    def status(value, **extra):
        if on_status:
            on_status(value, attempt, **extra)

    def console_message(message):
        if message.type in ("error", "warning"):
            log(f"{tag} console {message.type}: {message.text}")

    def request_failed(request):
        failure = request.failure or ""
        hostname = (urlparse(request.url).hostname or "").lower()
        if not hostname.endswith("vectorizer.ai") or (
            "/download?" in request.url and "ERR_ABORTED" in failure
        ):
            return
        log(f"{tag} request failed: {request.method} {request.url} {failure}")

    def response_received(response):
        hostname = (urlparse(response.url).hostname or "").lower()
        if hostname.endswith("vectorizer.ai") and response.status in (401, 403, 429):
            service_responses.append((response.status, response.url))

    async def enforce_service_response():
        if not service_responses:
            return
        status_code, url = service_responses[-1]
        if status_code == 429:
            reason = f"Vectorizer.ai rate limited the session (HTTP 429): {url}"
            await SERVICE_CIRCUIT.block(reason, RATE_LIMIT_COOLDOWN_SECONDS)
            raise RateLimitError(reason)
        reason = f"Vectorizer.ai rejected the session (HTTP {status_code}): {url}"
        request_profile_refresh(reason, url)
        await SERVICE_CIRCUIT.block(reason, SERVICE_BLOCK_COOLDOWN_SECONDS)
        raise NonRetryableServiceError(reason)

    async def reject_redirect(current_url, stage):
        if is_account_redirect_url(current_url):
            reason = f"Vectorizer.ai redirected to {current_url} during {stage}"
            request_profile_refresh(reason, current_url)
            await SERVICE_CIRCUIT.block(reason, SERVICE_BLOCK_COOLDOWN_SECONDS)
            raise NonRetryableServiceError(reason)

    def page_error(error):
        log(f"{tag} page error: {error}")

    page.on("pageerror", page_error)
    page.on("console", console_message)
    page.on("requestfailed", request_failed)
    page.on("response", response_received)

    try:
        await SERVICE_CIRCUIT.wait_for_request_slot()
        status("opening")
        try:
            await page.bring_to_front()
        except Exception:
            pass
        log(f"{tag} opening home")
        if "vectorizer.ai" not in page.url or is_account_redirect_url(page.url):
            await page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
            await page.wait_for_timeout(900)
        else:
            await _return_to_home(page)
        await enforce_service_response()
        await reject_redirect(page.url, "page load")

        # Quick auth smoke-test: #user_email should be visible
        try:
            email_el = page.locator("#user_email").first
            if not await email_el.is_visible(timeout=3000):
                log(f"{tag} WARNING: #user_email not visible — may not be logged in")
        except Exception:
            log(f"{tag} WARNING: #user_email check failed — session may be stale")

        status("uploading")
        try:
            await page.mouse.move(260, 220)
            await page.wait_for_timeout(250)
        except Exception:
            pass
        await page.locator(S.FILE_INPUT).first.set_input_files(str(file_path))
        log(f"{tag} uploaded ({file_size / 1024:.0f}KB)")

        start_poll = time.monotonic()
        last_change = start_poll
        last_warning = 0
        status("converting", stall_seconds=0)

        while True:
            await enforce_service_response()
            await reject_redirect(page.url, "conversion")
            try:
                pct_data = await asyncio.wait_for(
                    page.evaluate(
                        """() => {
                            function pct(el) {
                                if (!el || !el.style.width) return 0;
                                const value = parseFloat(el.style.width);
                                return isNaN(value) ? 0 : Math.round(value * 100) / 100;
                            }
                            return JSON.stringify({
                                upload: pct(document.querySelector('#App-Progress-Upload-Bar')),
                                process: pct(document.querySelector('#App-Progress-Process-Bar')),
                                fetch: pct(document.querySelector('#App-Progress-Download-Bar')),
                            });
                        }"""
                    ),
                    timeout=DOM_CALL_TIMEOUT_MS / 1000,
                )
            except asyncio.TimeoutError as error:
                raise TimeoutError(
                    "Browser renderer stopped responding while reading progress"
                ) from error

            try:
                bars = json.loads(pct_data)
            except Exception:
                bars = {"upload": 0, "process": 0, "fetch": 0}

            now = time.monotonic()
            if bars != last_bars:
                last_bars = bars
                last_change = now
                last_warning = 0
                if on_progress:
                    on_progress(bars)

            stalled_for = now - last_change
            status("converting", stall_seconds=int(stalled_for))
            if stalled_for >= STALL_WARN_SECONDS and (
                not last_warning or now - last_warning >= STALL_WARN_SECONDS
            ):
                log(
                    f"{tag} STALL WARNING: unchanged for {stalled_for:.0f}s; "
                    f"bars={bars}"
                )
                last_warning = now
            if stalled_for >= STALL_TIMEOUT_SECONDS:
                raise TimeoutError(
                    f"Progress stalled for {stalled_for:.0f}s at {bars}"
                )

            try:
                if await page.get_by_text("Network Error", exact=True).is_visible(timeout=250):
                    raise RuntimeError(
                        "Vectorizer.ai could not connect to its conversion worker"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass

            if bars.get("fetch", 0) >= 100:
                download_ready = await asyncio.wait_for(
                    page.evaluate(
                        "() => { const d = document.querySelector('#App-DownloadLink');"
                        " return Boolean(d && !d.hasAttribute('disabled')); }"
                    ),
                    timeout=DOM_CALL_TIMEOUT_MS / 1000,
                )
                if download_ready:
                    break

            if now - start_poll > CONVERSION_TIMEOUT_MS / 1000:
                raise TimeoutError("Conversion timed out")
            await asyncio.sleep(1.5)

        status("downloading", stall_seconds=0)
        log(f"{tag} conversion done")

        await enforce_service_response()
        await reject_redirect(page.url, "download preparation")

        await page.evaluate(
            "() => {"
            " const m = document.querySelector('#App-Progress-Dialog');"
            " if (m) { m.classList.remove('in'); m.style.display = 'none'; }"
            " const b = document.querySelector('.modal-backdrop');"
            " if (b) { b.classList.remove('in'); b.style.display = 'none'; }"
            "}"
        )
        await page.wait_for_timeout(500)
        await page.evaluate(
            """() => {
                const link = document.querySelector('#App-DownloadLink');
                if (!link) throw new Error('Download options link not found');
                link.click();
            }"""
        )
        await page.locator(S.OPTIONS_DOWNLOAD).first.wait_for(
            state="attached", timeout=DOWNLOAD_TIMEOUT_MS
        )

        for selector in (S.CHECK_INCLUDE_IMAGES, S.CHECK_INCLUDE_COLORS):
            try:
                checkbox = page.locator(selector).first
                if await checkbox.is_visible() and not await checkbox.is_checked():
                    await checkbox.check()
            except Exception:
                pass

        try:
            await page.locator(S.SLIDER_PRECISION).first.evaluate(
                "(el) => { el.value = el.max;"
                " el.dispatchEvent(new Event('input', {bubbles: true}));"
                " el.dispatchEvent(new Event('change', {bubbles: true})); }"
            )
        except Exception:
            pass

        if OUTPUT_FORMAT in ("SVG", "PDF", "EPS", "DXF", "PNG"):
            try:
                await page.get_by_text(OUTPUT_FORMAT, exact=True).first.click()
            except Exception:
                pass

        await page.wait_for_timeout(300)

        await enforce_service_response()
        await reject_redirect(page.url, "download")

        try:
            async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
                await page.locator(S.OPTIONS_DOWNLOAD).first.evaluate("(el) => el.click()")
            download = await download_info.value
        except Exception as error:
            await enforce_service_response()
            try:
                await reject_redirect(page.url, "download")
            except NonRetryableServiceError as block_error:
                raise block_error from error
            raise
        extension = (
            download.suggested_filename.split(".")[-1]
            if download.suggested_filename else "svg"
        )
        out_path = OUTPUT_DIR / f"{file_path.stem}.{extension.lower()}"
        await download.save_as(out_path)
        return out_path
    except Exception as error:
        await _save_diagnostics(page, file_path, attempt, error, last_bars)
        raise
    finally:
        for event, handler in (
            ("pageerror", page_error),
            ("console", console_message),
            ("requestfailed", request_failed),
            ("response", response_received),
        ):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass


async def process_one(context, sem, file_path, idx, total,
                      on_done=None, on_fail=None, on_progress=None, on_start=None,
                      on_status=None):
    """Process one file with fresh-page retries and stall detection."""
    started = time.monotonic()
    file_size = file_path.stat().st_size

    async with sem:
        if on_start:
            on_start(file_path, file_size)
        log(f"[{idx}/{total}] START {file_path.name}; retries={MAX_RETRIES}")
        last_error = None

        for attempt in range(1, MAX_RETRIES + 2):
            page = await _get_work_page(context)
            if on_progress:
                on_progress({"upload": 0, "process": 0, "fetch": 0})
            try:
                out_path = await _process_attempt(
                    context, file_path, idx, total, attempt, on_progress, on_status, page
                )
                await _release_work_page(page)
                elapsed = time.monotonic() - started
                log(f"[{idx}/{total}] SAVED -> {out_path.name} ({elapsed:.0f}s)")
                if on_done:
                    on_done(file_path, out_path, elapsed, file_size)
                return {
                    "ok": True,
                    "path": out_path,
                    "elapsed": elapsed,
                    "file_size": file_size,
                    "attempts": attempt,
                }
            except Exception as error:
                await _close_work_page(page)
                last_error = error
                log(
                    f"[{idx}/{total}] attempt {attempt}/{MAX_RETRIES + 1} failed: "
                    f"{type(error).__name__}: {error}"
                )
                log(f"[{idx}/{total}] traceback:\n{traceback.format_exc().rstrip()}")
                if isinstance(error, NonRetryableServiceError):
                    log(f"[{idx}/{total}] not retrying a service/account rejection")
                    break
                if attempt <= MAX_RETRIES:
                    delay = RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                    delay += random.uniform(0, max(0, RETRY_JITTER_SECONDS))
                    if on_status:
                        on_status("retrying", attempt + 1, retry_in=int(delay))
                    log(f"[{idx}/{total}] retrying in {delay:.1f}s with another page")
                    await asyncio.sleep(delay)

        elapsed = time.monotonic() - started
        error_text = f"{type(last_error).__name__}: {last_error}"
        log(f"[{idx}/{total}] FAILED after {attempt} attempt(s) ({elapsed:.0f}s)")
        if on_fail:
            on_fail(file_path, error_text, elapsed)
        return {
            "ok": False,
            "error": error_text,
            "elapsed": elapsed,
            "attempts": attempt,
        }
