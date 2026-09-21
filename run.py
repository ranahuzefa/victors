"""
run.py - vectorizer.ai batch automation
Uses persistent browser profile so you only log in ONCE.
"""

import os
import sys
import traceback
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from av_selectors import S
from auth import ensure_logged_in

load_dotenv()
EMAIL = os.environ['VECTORIZER_EMAIL']
PASSWORD = os.environ['VECTORIZER_PASSWORD']
INPUT_DIR = Path(os.environ['INPUT_DIR'])
OUTPUT_DIR = Path(os.environ['OUTPUT_DIR'])
OUTPUT_FORMAT = os.environ.get('OUTPUT_FORMAT', 'SVG').upper()

HEADLESS = False
SLOW_MO_MS = 0
CONVERSION_TIMEOUT_MS = 5 * 60_000
DOWNLOAD_TIMEOUT_MS = 120_000

SUPPORTED_EXT = {'.jpg', '.jpeg', '.png', '.webp'}
PROFILE_DIR = OUTPUT_DIR / '.browser_profile'


def log(*args):
    print('[vectorizer]', *args, flush=True)


def list_inputs():
    if not INPUT_DIR.exists():
        raise FileNotFoundError(f'INPUT_DIR does not exist: {INPUT_DIR}')
    files = sorted(
        p for p in INPUT_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXT
    )
    if not files:
        raise FileNotFoundError(f'No JPG/PNG/WEBP files in {INPUT_DIR}')
    return files


def safe_filename(path, suffix):
    return f'{path.stem}.{suffix.lower()}'


def get_format_locator(page, fmt):
    return {
        'SVG': lambda: page.get_by_text('SVG', exact=True).first,
        'PDF': lambda: page.get_by_text('PDF', exact=True).first,
        'EPS': lambda: page.get_by_text('EPS', exact=True).first,
        'DXF': lambda: page.get_by_text('DXF', exact=True).first,
        'PNG': lambda: page.get_by_text('PNG', exact=True).first,
    }.get(fmt.upper())


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    inputs = list_inputs()
    log(f'Found {len(inputs)} input file(s) in {INPUT_DIR}')
    with sync_playwright() as p:
        # Persistent context — cookies survive across runs
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            slow_mo=SLOW_MO_MS,
            accept_downloads=True,
            viewport={'width': 1440, 'height': 900},
            args=['--disable-blink-features=AutomationControlled'],
        )
        page = context.new_page()
        page.set_default_timeout(20000)

        try:
            # 1) Homepage
            log('Opening vectorizer.ai ...')
            page.goto('https://vectorizer.ai/', wait_until='domcontentloaded')
            page.wait_for_timeout(2000)

            # 2) Login (see auth.py — never edit this call)
            ensure_logged_in(page, PROFILE_DIR)

            # 3-N) Process each file
            for idx, file_path in enumerate(inputs, start=1):
                log(f'--- [{idx}/{len(inputs)}] {file_path.name} ---')

                # Clean up any leftover modals from previous file
                page.evaluate(
                    "() => {"
                    " document.querySelectorAll('.modal-backdrop').forEach("
                    "  el => el.remove()"
                    " );"
                    "}"
                )
                page.wait_for_timeout(300)

                # Upload — feed the hidden file input directly
                page.locator(S.FILE_INPUT).first.set_input_files(str(file_path))
                log(' Uploaded, waiting for conversion ...')

                # Wait for the Fetch progress bar to reach 100%
                # The modal closes automatically when done — we wait for it to disappear
                log(' Processing (Upload → Process → Fetch)...')
                page.wait_for_function(
                    """() => {
                        const bar = document.querySelector('#App-Progress-Download-Bar');
                        if (!bar) return true;  // no bar = already done
                        return bar.style.width === '100%';
                    }""",
                    timeout=CONVERSION_TIMEOUT_MS,
                )
                # Now wait for the modal to close (or the download link to become clickable)
                page.wait_for_selector(
                    S.CONVERSION_DONE,
                    state='visible',
                    timeout=CONVERSION_TIMEOUT_MS,
                )
                page.wait_for_timeout(1000)
                log(' Conversion finished.')

                # Step 1: Click "DOWNLOAD" to go to the options page
                dl_link = page.locator(S.GO_TO_OPTIONS).first
                dl_link.click()
                page.wait_for_load_state('networkidle')
                page.wait_for_timeout(1500)
                log(' Navigated to options page.')

                # Step 2: Set options on the /images/... page
                for sel in (S.CHECK_INCLUDE_IMAGES, S.CHECK_INCLUDE_COLORS):
                    try:
                        chk = page.locator(sel).first
                        if chk.is_visible() and not chk.is_checked():
                            chk.check()
                    except Exception as e:
                        log(f' toggle skipped: {e}')

                try:
                    page.locator(S.SLIDER_PRECISION).first.evaluate(
                        "(el) => { el.value = el.max;"
                        " el.dispatchEvent(new Event('input', {bubbles: true}));"
                        " el.dispatchEvent(new Event('change', {bubbles: true})); }"
                    )
                except Exception as e:
                    log(f' precision slider skipped: {e}')

                fmt_loc = get_format_locator(page, OUTPUT_FORMAT)
                if fmt_loc:
                    try:
                        fmt_loc().click()
                        log(f' Selected format: {OUTPUT_FORMAT}')
                    except Exception as e:
                        log(f' Could not click format {OUTPUT_FORMAT}: {e}')

                # Step 3: Click #Options-Submit via JS (button may be below fold)
                log(' Clicking Options-Submit...')
                page.wait_for_timeout(500)
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
                    page.evaluate('document.getElementById("Options-Submit").click()')
                download = dl_info.value
                ext = download.suggested_filename.split('.')[-1] if download.suggested_filename else 'svg'
                out_path = OUTPUT_DIR / safe_filename(file_path, ext)
                download.save_as(out_path)
                log(f' Saved -> {out_path}')

                # Go back to homepage for the next file
                page.goto('https://vectorizer.ai/', wait_until='domcontentloaded')
                page.wait_for_timeout(1500)

            log('All done.')

        except Exception:
            log('FAILED - full traceback:')
            traceback.print_exc()
            try:
                page.screenshot(
                    path=str(OUTPUT_DIR / 'debug_screenshot.png'),
                    full_page=True,
                )
                with open(OUTPUT_DIR / 'debug_page.html', 'w', encoding='utf-8') as f:
                    f.write(page.content())
                log(f'Debug artifacts saved to {OUTPUT_DIR}')
            except Exception:
                pass
            sys.exit(1)
        finally:
            context.close()


if __name__ == '__main__':
    main()
