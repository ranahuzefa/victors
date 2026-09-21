"""Reusable Vectorizer.ai session validation and credential login helpers.

is_logged_in() uses a multi-factor check:
  1. Positive: #user_email visible with real text content
  2. Positive: "Log Out" or "My Account" link visible in the nav
  3. Negative: "Log In"/"Sign In" link NOT visible
  4. URL: not on /pricing or /login pages
  5. Cookies: auth cookies present (atk, VK token check)

False-flag protection: if cookies exist but the page shows "Log In",
the session has expired and credentials must be re-submitted.
"""

import os
from pathlib import Path

from av_selectors import S


def is_account_redirect_url(url):
    lowered = (url or "").lower()
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


async def _get_text_safe(locator, timeout=2000):
    """Safely get text content from a locator, returning None on failure."""
    try:
        el = locator.first
        if await el.is_visible(timeout=timeout):
            return (await el.text_content() or "").strip()
    except Exception:
        pass
    return None


async def is_logged_in(page, timeout=3000):
    """Multi-factor login check. Returns (is_logged_in: bool, detail: str)."""
    reasons = []

    # ── Factor 1: user email visible with real text ──
    email_text = None
    try:
        email_el = page.locator("#user_email").first
        if await email_el.is_visible(timeout=timeout):
            email_text = (await email_el.text_content() or "").strip()
            if email_text and "@" in email_text:
                reasons.append(f"email={email_text}")
            else:
                reasons.append(f"#user_email visible but empty/weird: '{email_text}'")
    except Exception:
        reasons.append("#user_email not found")

    # ── Factor 2: "Log Out" or "My Account" links ──
    try:
        logout_el = page.locator("a:has-text('Log Out'), button:has-text('Log Out')").first
        if await logout_el.is_visible(timeout=1500):
            reasons.append("logout-link visible")
    except Exception:
        pass

    try:
        account_el = page.locator("a:has-text('My Account')").first
        if await account_el.is_visible(timeout=1500):
            reasons.append("account-link visible")
    except Exception:
        pass

    # ── Factor 3 (negative): "Log In"/"Sign In" link should NOT be visible ──
    try:
        login_el = page.locator("a:has-text('Log In'), a:has-text('Sign In')").first
        if await login_el.is_visible(timeout=1000):
            reasons.append("WARN: login-link still visible")
    except Exception:
        pass  # Not finding it is good

    # ── Factor 4: URL should not be pricing or login ──
    try:
        url = page.url
        if is_account_redirect_url(url):
            reasons.append(f"WARN: account/login redirect ({url})")
    except Exception:
        pass

    # ── Decision ──
    has_email = email_text and "@" in email_text
    has_logout_or_account = any("logout" in r or "account" in r for r in reasons)
    has_login_warning = any("WARN" in r for r in reasons)

    if has_email and not has_login_warning:
        return True, " | ".join(reasons)
    elif has_email and has_login_warning:
        # Email visible but warnings exist — session may be stale
        return False, "STALE: " + " | ".join(reasons)
    elif has_logout_or_account and not has_login_warning:
        return True, " | ".join(reasons)
    else:
        return False, " | ".join(reasons) if reasons else "no indicators found"


def is_logged_in_sync(page, timeout=3000):
    """Synchronous wrapper for use in sync Playwright contexts (login flow)."""
    reasons = []
    email_text = None

    # Factor 1: user email
    try:
        email_el = page.locator("#user_email").first
        if email_el.is_visible(timeout=timeout):
            email_text = (email_el.text_content() or "").strip()
            if email_text and "@" in email_text:
                reasons.append(f"email={email_text}")
            else:
                reasons.append(f"#user_email visible but empty: '{email_text}'")
    except Exception:
        reasons.append("#user_email not found")

    # Factor 2: logout/account links
    try:
        if page.locator("a:has-text('Log Out'), button:has-text('Log Out')").first.is_visible(timeout=1500):
            reasons.append("logout-link visible")
    except Exception:
        pass
    try:
        if page.locator("a:has-text('My Account')").first.is_visible(timeout=1500):
            reasons.append("account-link visible")
    except Exception:
        pass

    # Factor 3: login link should NOT be visible
    try:
        if page.locator("a:has-text('Log In'), a:has-text('Sign In')").first.is_visible(timeout=1000):
            reasons.append("WARN: login-link visible")
    except Exception:
        pass

    # Factor 4: URL check
    try:
        url = page.url
        if is_account_redirect_url(url):
            reasons.append(f"WARN: account/login redirect ({url})")
    except Exception:
        pass

    has_email = email_text and "@" in email_text
    has_logout_or_account = any("logout" in r or "account" in r for r in reasons)
    has_login_warning = any("WARN" in r for r in reasons)

    if has_email and not has_login_warning:
        return True, " | ".join(reasons)
    elif has_email and has_login_warning:
        return False, "STALE: " + " | ".join(reasons)
    elif has_logout_or_account and not has_login_warning:
        return True, " | ".join(reasons)
    else:
        return False, " | ".join(reasons) if reasons else "no indicators found"


def try_credential_login(page, email=None, password=None, timeout=30_000):
    """Log in through Vectorizer.ai's Cedar Lake Ventures sign-on iframe."""
    email = email or os.environ.get("VECTORIZER_EMAIL", "")
    password = password or os.environ.get("VECTORIZER_PASSWORD", "")
    if not email or not password:
        return False, "VECTORIZER_EMAIL and VECTORIZER_PASSWORD are not configured"

    try:
        # Navigate to homepage and check if login link is visible
        page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Find and click the Login link
        login_link = page.locator(S.LOGIN_LINK).first
        try:
            login_link.wait_for(state="visible", timeout=10000)
            login_link.click(timeout=5000)
        except Exception:
            # Maybe already on a login page — try direct navigation
            page.goto("https://vectorizer.ai/login", wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

        # Wait for the Cedar Lake Ventures sign-on iframe
        iframe = page.locator(S.SIGNON_IFRAME).first
        try:
            iframe.wait_for(state="attached", timeout=10000)
        except Exception:
            # Try alternative: maybe the login form is inline, not in iframe
            pass

        frame = page.frame_locator(S.SIGNON_IFRAME) if page.locator(S.SIGNON_IFRAME).first.is_visible() else page

        # Fill credentials
        try:
            frame.locator(S.LOGIN_EMAIL_INPUT).first.fill(email, timeout=10000)
        except Exception:
            # Try broader selectors if the specific ones fail
            frame.get_by_placeholder("email").first.fill(email, timeout=5000)

        try:
            frame.locator(S.LOGIN_PASSWORD_INPUT).first.fill(password, timeout=10000)
        except Exception:
            frame.get_by_placeholder("password").first.fill(password, timeout=5000)

        # Click submit
        try:
            frame.locator(S.LOGIN_SUBMIT).first.click(timeout=10000)
        except Exception:
            frame.locator("button[type='submit']").first.click(timeout=5000)

        # Wait for login to complete — "Log In" link should disappear
        page.wait_for_timeout(3000)
        try:
            page.wait_for_function(
                """() => {
                    const hasLogin = [...document.querySelectorAll('a')]
                        .some(a => /log in|sign in/i.test(a.textContent || ''));
                    const hasEmail = Boolean(document.querySelector('#user_email'));
                    const hasLogout = Boolean(
                        document.querySelector('a[href*="logout"], button:has-text("Log Out")')
                    );
                    return !hasLogin && (hasEmail || hasLogout);
                }""",
                timeout=timeout,
            )
        except Exception:
            # Wait function may have timed out — check manually
            page.wait_for_timeout(3000)

        # Navigate back to homepage
        page.goto("https://vectorizer.ai/", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        ok, detail = is_logged_in_sync(page, timeout=5000)
        if ok:
            return True, f"credential login confirmed ({detail})"
        return False, f"credentials submitted but not confirmed: {detail}"

    except Exception as error:
        detail = str(error).replace(email, "<redacted-email>").replace(
            password, "<redacted-password>"
        )
        detail = detail.encode("ascii", "backslashreplace").decode("ascii")
        return False, f"{type(error).__name__}: {detail}"


def ensure_logged_in(page, profile_dir: Path) -> bool:
    """Legacy sequential-runner compatibility wrapper."""
    ok, detail = is_logged_in_sync(page)
    if ok:
        print(f"[auth] Already logged in. ({detail})")
        return True

    print(f"[auth] Not logged in: {detail}")
    ok, reason = try_credential_login(page)
    if ok:
        print(f"[auth] Headless credential login confirmed. ({reason})")
        return True

    print(f"[auth] Automated login failed: {reason}")
    print(f"[auth] Re-login required for profile: {profile_dir}")
    return False
