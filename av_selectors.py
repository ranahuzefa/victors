"""
av_selectors.py — single source of truth for every CSS selector / text query.
If vectorizer.ai re-skins, update here and nothing else breaks.
"""

class S:
 # ── top nav ─────────────────────────────────────────────────────────
 LOGIN_LINK = "a:has-text('Log In'), a:has-text('Sign In')"
 SIGNON_IFRAME = "#Signon-IframeContainer iframe, iframe[src*='cedarlakeventures.com/signon']"
 LOGIN_EMAIL_INPUT = "#login_email"
 LOGIN_PASSWORD_INPUT = "#login_password"
 LOGIN_SUBMIT = "button[type='submit']:visible"

 # ── logged-in indicator (skip login if this is visible) ─────────────
 LOGGED_IN = "#user_email, a:has-text('My Account'), a:has-text('Log Out'), button:has-text('Log Out')"

 # ── login form (vectorizer.ai direct + cedarlakeventures auth) ──────
 # Broad enough to hit almost any login form
 EMAIL_INPUT = (
 "input[type='email'], input[name*='email' i], "
 "input[name='username'], input[id*='email' i], "
 "input[placeholder*='email' i], input[placeholder*='Email' i], "
 "input[aria-label*='email' i], input[autocomplete='email'], "
 "input[autocomplete='username']"
 )
 PASSWORD_INPUT = "input[type='password'], input[name*='password' i], input[placeholder*='password' i]"
 SUBMIT_BTN = (
 "button[type='submit'], input[type='submit'], "
 "button:has-text('Log in'), button:has-text('Sign in'), button:has-text('Continue'), "
 "button:has-text('Log In'), button:has-text('Sign In'), "
 "input[value='Log In'], input[value='Sign In'], "
 "a:has-text('Log in'), a:has-text('Sign in')"
 )

 # ── home / upload ───────────────────────────────────────────────────
 HOME_LINK = "header a[href='/'], nav a[href='/'], a.navbar-brand, a.logo, a:has(img[alt*='Vectorizer' i])"
 # Visible primary upload button
 PICK_IMAGE_BTN = "button:has-text('Pick image'), button:has-text('PICK IMAGE'), :text('Drag & drop')"
 # The actual <input type="file"> (hidden behind the styled dropzone)
 FILE_INPUT = "input[type='file']"

 # ── result page — status / conversion-finished cues ────────────────
 # App-DownloadLink appears only after a successful conversion
 CONVERSION_DONE = "#App-DownloadLink, .Viewer-blue_button.isPaid"
 # First click: go from thumbnail page to the /images/... options page
 GO_TO_OPTIONS = "#App-DownloadLink"
 # Second click: actual download on the /images/... options page
 OPTIONS_DOWNLOAD = "#Options-Submit, button[type='submit']:has-text('DOWNLOAD')"
 # Fallback
 DOWNLOAD_HREF = "a[download], a[href$='.svg'], a[href$='.pdf'], a[href$='.eps'], a[href$='.dxf']"

 # ── result page — options (use has-text / regex for resilience) ─────
 FORMAT_SVG = ":text-is('SVG'), :text-is('svg')"
 FORMAT_PDF = ":text-is('PDF'), :text-is('pdf')"
 FORMAT_EPS = ":text-is('EPS'), :text-is('eps')"
 FORMAT_DXF = ":text-is('DXF'), :text-is('dxf')"

 CHECK_INCLUDE_IMAGES = "input[name*='include' i]"
 CHECK_INCLUDE_COLORS = "input[name*='color' i]"
 SLIDER_PRECISION = "input[type='range']"
 SELECT_PALETTE = "select[name*='palette' i], select[name*='color' i]"
