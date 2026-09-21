# AutoVector

Headless batch automation for [Vectorizer.ai](https://vectorizer.ai/).

AutoVector uploads JPG, PNG, and WebP images, monitors live conversion progress,
and downloads SVG, PDF, EPS, DXF, or PNG results. It uses a persistent browser
profile, concurrent Playwright pages, automatic retries, stuck-job detection,
and a fixed terminal dashboard.

> This project is not affiliated with Vectorizer.ai. Use it responsibly and in
> accordance with the service's terms, account limits, and rate limits.

## Features

- Headless Chromium processing after a one-time manual login
- Concurrent image conversion with configurable limits
- Stable per-image progress bars and an overall batch bar
- Automatic stuck-job detection and fresh-page retries
- Resume support by skipping existing output files
- Failure screenshots and diagnostic JSON
- Human-readable logs and machine-readable JSONL progress snapshots
- Optional desktop, Discord, and Telegram notifications

## Requirements

- Python 3.10 or newer
- A Vectorizer.ai account with download access
- Chromium installed through Playwright

Install:

```powershell
git clone https://github.com/RanaAhmadGulabi/autovector.git
cd autovector

py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

On macOS or Linux, activate the environment with:

```bash
source .venv/bin/activate
```

## Configuration

Create your local configuration from the example:

```powershell
Copy-Item .env.example .env
```

macOS/Linux:

```bash
cp .env.example .env
```

Important settings:

```ini
INPUT_DIR=./input
OUTPUT_DIR=./output
OUTPUT_FORMAT=SVG

MAX_CONCURRENT=2
MAX_SAFE_CONCURRENT=2
HEADLESS=true
BROWSER_CHANNEL=chromium

CONVERSION_TIMEOUT_MS=300000
DOWNLOAD_TIMEOUT_MS=120000
DOM_CALL_TIMEOUT_MS=15000

STALL_WARN_SECONDS=30
STALL_TIMEOUT_SECONDS=90
MAX_RETRIES=2
RETRY_DELAY_SECONDS=5
RETRY_JITTER_SECONDS=2

MIN_REQUEST_INTERVAL_SECONDS=8
RATE_LIMIT_COOLDOWN_SECONDS=300
SERVICE_BLOCK_COOLDOWN_SECONDS=900
```

`MAX_RETRIES=2` allows three total attempts. Retry delays use exponential
backoff starting at `RETRY_DELAY_SECONDS`. Request starts are spaced by
`MIN_REQUEST_INTERVAL_SECONDS`. HTTP 429 responses and account/login/pricing
rejections open a shared cooldown circuit and are not retried.

Do not commit `.env`. It is ignored because notification tokens and other local
settings may be sensitive.

## Usage

1. Put source images in `input/`.
2. Run the batch processor:

```powershell
python runner.py
```

On the first run, AutoVector uses `VECTORIZER_EMAIL` and
`VECTORIZER_PASSWORD` to attempt the Cedar Lake Ventures iframe login
headlessly. The authenticated session is stored under
`output/.browser_profile/`, and later runs validate the saved profile
headlessly.

If automated login fails because of CAPTCHA, 2FA, or an SSO change,
`MANUAL_LOGIN_FALLBACK=true` opens a visible browser. On a remote Linux server
without a desktop, set `MANUAL_LOGIN_FALLBACK=false` so the runner exits with a
clear authentication error instead of trying to open a window.

Existing output files larger than 100 bytes are skipped. Reprocess everything:

```powershell
python runner.py --force
```

## Live Dashboard

The terminal dashboard updates in place. Each active image gets a fixed row,
and overall progress stays at the bottom:

```text
image-a.png  [############------------------]   Proc 40%  43.0%
image-b.png  [######################--------]   Proc 88%  76.5%
image-c.png  [###---------------------------]  STUCK 35s  10.2%
OVERALL 4/12 [########----------------------]             28.7% | ETA:3m12s
```

`STUCK Ns` appears after `STALL_WARN_SECONDS`. If unchanged progress reaches
`STALL_TIMEOUT_SECONDS`, the attempt is stopped and retried in a fresh page.
Retry rows display `Retry 2`, `A2`, and similar attempt indicators.

Detailed diagnostics stay in log files so they do not interrupt the dashboard.

## Remote Linux and SSH

AutoVector can run entirely headlessly on a Linux server:

```bash
git clone https://github.com/RanaAhmadGulabi/autovector.git
cd autovector
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium
cp .env.example .env
python runner.py
```

For unattended authentication, configure:

```ini
HEADLESS=true
AUTO_LOGIN=true
MANUAL_LOGIN_FALLBACK=false
VECTORIZER_EMAIL=you@example.com
VECTORIZER_PASSWORD=your-password
```

Credentials are only used to fill the cross-origin sign-on iframe. Successful
login cookies are persisted under `output/.browser_profile/`.

## Runtime Commands

Enter commands while `runner.py` is running:

| Command | Action |
|---|---|
| `p` / `pause` | Pause before starting more work |
| `r` / `resume` | Resume processing |
| `s N` / `skip N` | Skip file number N |
| `c N` / `concurrent N` | Change concurrency |
| `q` / `status` | Print current status |
| `stop` | Stop after in-flight work |
| `h` / `help` | Show command help |

## Logs and Diagnostics

Each run creates:

| File | Purpose |
|---|---|
| `output/run_YYYYMMDD_HHMMSS.log` | Human-readable events and tracebacks |
| `output/run_YYYYMMDD_HHMMSS.jsonl` | Structured events and 10-second progress snapshots |
| `output/progress.json` | Latest batch state |
| `output/fail_<image>_attemptN.png` | Screenshot from a failed attempt |
| `output/fail_<image>_attemptN.json` | URL, error, attempt, and progress values |

## Project Layout

| Path | Purpose |
|---|---|
| `runner.py` | Recommended batch runner and terminal dashboard |
| `core.py` | Conversion, download, retry, and diagnostics workflow |
| `av_selectors.py` | Central Playwright selectors (renamed from `selectors.py` to avoid stdlib shadow on Python ≥ 3.14) |
| `run_parallel.py` | Minimal concurrent runner |
| `run.py` / `auth.py` | Legacy sequential fallback |
| `.env.example` | Public configuration template |
| `input/` | Local source images |
| `output/` | Downloads, browser profile, and logs |

## Bridge Dashboard Integration

AutoVector is one half of the [Bridge Dashboard](https://github.com/RanaAhmadGulabi/bridge) pipeline:

```
PNG → AutoVector (SVG) → Video FX (MP4)
```

The Bridge orchestrates both tools with a real-time WebSocket dashboard:
- AutoVector headless vectorization via Playwright
- Automatic SVG transfer to Video FX input
- Streaming pipeline: render starts as soon as first SVG is ready
- Per-job progress, ETA, duration tracking
- Safe stop, pause/resume, retry

Clone all three repos as siblings in the same workspace directory.

## Troubleshooting

| Symptom | Action |
|---|---|
| Login window opens in headless mode | Complete login once; the saved session is missing or expired |
| Upload stops around 5% | Keep the Chromium channel and normal Chrome user agent from `.env.example` |
| Images repeatedly become stuck | Lower `MAX_CONCURRENT`, increase `STALL_TIMEOUT_SECONDS`, and inspect diagnostics |
| Conversion or download times out | Increase the matching timeout and inspect failure screenshots |
| CAPTCHA or rate limiting | Set `MAX_CONCURRENT=1` and temporarily use `HEADLESS=false` |
| Output already exists | Use `python runner.py --force` |

High concurrency can cause queueing, CAPTCHA checks, or rate limiting. Start
with `MAX_CONCURRENT=2` and increase cautiously.

## Security

- Never commit `.env`, `output/.browser_profile/`, logs, source images, or
  downloaded outputs.
- The browser profile contains authenticated session data.
- Automated login stores credentials in `.env`; protect that file with
  appropriate filesystem permissions.
- CAPTCHA and 2FA are not bypassed. They require the manual fallback.
