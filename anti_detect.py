"""Consistent Playwright browser-profile hardening helpers."""

from __future__ import annotations

import inspect
import json
import ipaddress
import os
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--force-color-profile=srgb",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
]

SCREENS = [
    (1920, 1080),
    (1680, 1050),
    (1600, 900),
    (1536, 864),
    (1440, 900),
    (1366, 768),
    (1280, 1024),
]

GPUS = [
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 770 Direct3D11)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600 XT Direct3D11)"),
]

REQUESTED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.7680.178 Safari/537.36"
)
DEFAULT_CANVAS_SEED = 1467680178
CHROME_PDF_VIEWER_EXTENSION_ID = "mhjfbmdgcfjbbpaeojofohoefgiehjai"
DEFAULT_PROFILE_IP_GEO_URL = "http://ip-api.com/json/?fields=status,message,countryCode,lat,lon,timezone,query"

COUNTRY_LANGUAGE_DEFAULTS = {
    "US": ("en-US", ("en-US", "en")),
    "GB": ("en-GB", ("en-GB", "en")),
    "CA": ("en-CA", ("en-CA", "en-US", "en")),
    "AU": ("en-AU", ("en-AU", "en")),
    "DE": ("de-DE", ("de-DE", "de", "en-US", "en")),
    "FR": ("fr-FR", ("fr-FR", "fr", "en-US", "en")),
    "ES": ("es-ES", ("es-ES", "es", "en-US", "en")),
    "IT": ("it-IT", ("it-IT", "it", "en-US", "en")),
    "NL": ("nl-NL", ("nl-NL", "nl", "en-US", "en")),
    "BR": ("pt-BR", ("pt-BR", "pt", "en-US", "en")),
    "MX": ("es-MX", ("es-MX", "es", "en-US", "en")),
    "PK": ("en-PK", ("en-PK", "en-US", "en", "ur")),
    "IN": ("en-IN", ("en-IN", "en-US", "en", "hi")),
}

WINDOWS_FONT_FAMILIES = (
    "Arial",
    "Arial Black",
    "Bahnschrift",
    "Calibri",
    "Cambria",
    "Cambria Math",
    "Candara",
    "Comic Sans MS",
    "Consolas",
    "Constantia",
    "Corbel",
    "Courier New",
    "Ebrima",
    "Franklin Gothic Medium",
    "Gabriola",
    "Gadugi",
    "Georgia",
    "Impact",
    "Ink Free",
    "Javanese Text",
    "Leelawadee UI",
    "Lucida Console",
    "Lucida Sans Unicode",
    "Malgun Gothic",
    "Microsoft Himalaya",
    "Microsoft JhengHei",
    "Microsoft New Tai Lue",
    "Microsoft PhagsPa",
    "Microsoft Sans Serif",
    "Microsoft Tai Le",
    "Microsoft YaHei",
    "Microsoft Yi Baiti",
    "MingLiU-ExtB",
    "Mongolian Baiti",
    "MS Gothic",
    "MV Boli",
    "Myanmar Text",
    "Nirmala UI",
    "Palatino Linotype",
    "Segoe MDL2 Assets",
    "Segoe Print",
    "Segoe Script",
    "Segoe UI",
    "Segoe UI Emoji",
    "Segoe UI Historic",
    "Segoe UI Symbol",
    "SimSun",
    "Sitka",
    "Sylfaen",
    "Symbol",
    "Tahoma",
    "Times New Roman",
    "Trebuchet MS",
    "Verdana",
    "Webdings",
    "Wingdings",
    "Yu Gothic",
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_languages() -> tuple[str, ...]:
    raw = os.environ.get("PROFILE_LANGUAGES", "en-US,en")
    languages = tuple(language.strip() for language in raw.split(",") if language.strip())
    return languages or ("en-US", "en")


def _truthy_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_cached_ip_profile(cache_path: str, max_age_seconds: int) -> dict[str, Any] | None:
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        if time.time() - float(cached.get("fetched_at", 0)) > max_age_seconds:
            return None
        return cached.get("profile") if isinstance(cached.get("profile"), dict) else None
    except Exception:
        return None


def _write_cached_ip_profile(cache_path: str, profile: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump({"fetched_at": time.time(), "profile": profile}, handle)
    except Exception:
        pass


def _fetch_ip_profile() -> dict[str, Any]:
    if not _truthy_env("PROFILE_AUTO_IP_MATCH", False):
        return {}

    output_dir = os.environ.get("OUTPUT_DIR", "./output")
    cache_path = os.environ.get("PROFILE_IP_GEO_CACHE", os.path.join(output_dir, ".profile_ip_geo.json"))
    max_age = _env_int("PROFILE_IP_GEO_CACHE_SECONDS", 6 * 60 * 60)
    cached = _read_cached_ip_profile(cache_path, max_age)
    if cached:
        return cached

    url = os.environ.get("PROFILE_IP_GEO_URL", DEFAULT_PROFILE_IP_GEO_URL)
    timeout = max(0.25, _env_float("PROFILE_IP_GEO_TIMEOUT_SECONDS", 2.0))
    try:
        request = Request(url, headers={"User-Agent": REQUESTED_USER_AGENT})
        with urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("status") not in (None, "success"):
            return {}
        country_code = (data.get("countryCode") or data.get("country_code") or "").upper()
        locale, languages = COUNTRY_LANGUAGE_DEFAULTS.get(country_code, ("en-US", ("en-US", "en")))
        profile = {
            "locale": locale,
            "languages": list(languages),
            "timezone_id": data.get("timezone") or data.get("timezone_id") or "America/New_York",
            "geolocation_latitude": float(data.get("lat") or data.get("latitude")),
            "geolocation_longitude": float(data.get("lon") or data.get("longitude")),
            "geolocation_accuracy": _env_int("PROFILE_GEO_ACCURACY", 25000),
        }
        _write_cached_ip_profile(cache_path, profile)
        return profile
    except Exception:
        return {}


IP_PROFILE = _fetch_ip_profile()


@dataclass(frozen=True)
class Profile:
    """A coherent, process-stable desktop fingerprint."""

    width: int
    height: int
    avail_height: int
    hardware_concurrency: int
    device_memory: int
    webgl_vendor: str
    webgl_renderer: str
    browser: str = "Chrome"
    browser_match: str = "Auto-match"
    chrome_version: str = "146.0.7680.178"
    os_name: str = "Windows"
    platform: str = "Windows"
    platform_version: str = "10.0.0"
    architecture: str = "x86"
    bitness: str = "64"
    model: str = ""
    mobile: bool = False
    color_depth: int = 24
    device_scale_factor: int = 1
    timezone_mode: str = "ip"
    webrtc_mode: str = "privacy"
    geolocation_mode: str = "ip"
    language_mode: str = "ip"
    languages: tuple[str, ...] = ("en-US", "en")
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    geolocation_latitude: float = 40.7128
    geolocation_longitude: float = -74.0060
    geolocation_accuracy: int = 25000
    resolution_mode: str = "authentic"
    font_mode: str = "authentic"
    webgpu_mode: str = "webgl-based"
    hardware_acceleration: bool = True
    speech_voices_mode: str = "privacy"
    device_name: str = "DESKTOP-GIBJDLL"
    mac_address: str = "4F:E9:13:17:D8:11"
    do_not_track: bool = False
    bluetooth_mode: str = "privacy"
    battery_mode: str = "privacy"
    port_scan_protection: bool = True
    storage_quota: int = 8 * 1024 * 1024 * 1024
    storage_usage: int = 400 * 1024 * 1024
    connection_effective_type: str = "4g"
    connection_rtt: int = 50
    connection_downlink: float = 10.0
    max_touch_points: int = 0
    font_families: tuple[str, ...] = WINDOWS_FONT_FAMILIES
    canvas_seed: int = field(default=0, repr=False)
    runtime_id: str = field(default="", repr=False)

    @classmethod
    def generate(cls, rng: random.Random | None = None) -> "Profile":
        rng = rng or random.SystemRandom()
        width, height = rng.choice(SCREENS)
        vendor, renderer = rng.choice(GPUS)
        return cls(
            width=width,
            height=height,
            avail_height=height - rng.choice((40, 48)),
            hardware_concurrency=rng.choice((4, 8, 12, 16)),
            device_memory=rng.choice((4, 8)),
            webgl_vendor=vendor,
            webgl_renderer=renderer,
            canvas_seed=rng.randrange(1, 2**31),
            runtime_id="".join(rng.choice("abcdefghijklmnop") for _ in range(32)),
        )

    @property
    def viewport(self) -> dict[str, int]:
        return {"width": self.width, "height": self.height}

    @property
    def user_agent(self) -> str:
        return REQUESTED_USER_AGENT

    @property
    def chrome_major(self) -> int:
        return int(self.chrome_version.split(".", 1)[0])

    def script_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["user_agent"] = self.user_agent
        payload["chrome_major"] = self.chrome_major
        return payload


DEFAULT_PROFILE = Profile(
    width=1920,
    height=1080,
    avail_height=1032,
    hardware_concurrency=4,
    device_memory=8,
    webgl_vendor="Google Inc. (Intel)",
    webgl_renderer="ANGLE (Intel, Intel(R) UHD Graphics (0x00004C8A) Direct3D11 vs_5_0 ps_5_0, D3D11)",
    locale=IP_PROFILE.get("locale") or os.environ.get("PROFILE_LOCALE", "en-US"),
    languages=tuple(IP_PROFILE.get("languages") or _env_languages()),
    timezone_id=IP_PROFILE.get("timezone_id") or os.environ.get("PROFILE_TIMEZONE", "America/New_York"),
    geolocation_latitude=IP_PROFILE.get("geolocation_latitude") or _env_float("PROFILE_GEO_LATITUDE", 40.7128),
    geolocation_longitude=IP_PROFILE.get("geolocation_longitude") or _env_float("PROFILE_GEO_LONGITUDE", -74.0060),
    geolocation_accuracy=_env_int("PROFILE_GEO_ACCURACY", 25000),
    canvas_seed=DEFAULT_CANVAS_SEED,
    runtime_id=CHROME_PDF_VIEWER_EXTENSION_ID,
)
VIEWPORTS = [{"width": width, "height": height} for width, height in SCREENS]


def random_viewport(profile: Profile = DEFAULT_PROFILE) -> dict[str, int]:
    """Return a copy of the stable viewport selected for this process."""
    return profile.viewport.copy()


def build_stealth_script(profile: Profile = DEFAULT_PROFILE) -> str:
    payload = json.dumps(profile.script_payload(), separators=(",", ":"))
    return (
        "(() => {\n"
        f"const profile = Object.freeze({payload});\n"
        + _STEALTH_SCRIPT_BODY
        + "\n})();"
    )


_STEALTH_SCRIPT_BODY = r"""
const installMarker = Symbol.for('autovector.profile.installed');
if (window[installMarker]) return;
Object.defineProperty(window, installMarker, {value: true});

const defineGetter = (obj, name, getter) => {
    try {
        Object.defineProperty(obj, name, {
            get: getter, configurable: true, enumerable: true,
        });
    } catch (_) {}
};
const resolved = (value) => Promise.resolve(value);
const rejected = (name, message) => Promise.reject(new DOMException(message, name));

defineGetter(Navigator.prototype, 'webdriver', () => undefined);
defineGetter(Navigator.prototype, 'hardwareConcurrency', () => profile.hardware_concurrency);
defineGetter(Navigator.prototype, 'deviceMemory', () => profile.device_memory);
defineGetter(Navigator.prototype, 'userAgent', () => profile.user_agent);
defineGetter(Navigator.prototype, 'platform', () => 'Win32');
defineGetter(Navigator.prototype, 'language', () => profile.languages[0]);
defineGetter(Navigator.prototype, 'languages', () => Object.freeze([...profile.languages]));
defineGetter(Navigator.prototype, 'doNotTrack', () => profile.do_not_track ? '1' : null);
defineGetter(Navigator.prototype, 'cookieEnabled', () => true);
defineGetter(Navigator.prototype, 'pdfViewerEnabled', () => true);
defineGetter(Navigator.prototype, 'maxTouchPoints', () => profile.max_touch_points);
defineGetter(Screen.prototype, 'width', () => profile.width);
defineGetter(Screen.prototype, 'height', () => profile.height);
defineGetter(Screen.prototype, 'availWidth', () => profile.width);
defineGetter(Screen.prototype, 'availHeight', () => profile.avail_height);
defineGetter(Screen.prototype, 'colorDepth', () => profile.color_depth);
defineGetter(Screen.prototype, 'pixelDepth', () => profile.color_depth);
defineGetter(window, 'outerWidth', () => profile.width);
defineGetter(window, 'outerHeight', () => profile.height);
defineGetter(window, 'screenX', () => 0);
defineGetter(window, 'screenY', () => 0);
defineGetter(window, 'devicePixelRatio', () => profile.device_scale_factor);

const originalResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
Intl.DateTimeFormat.prototype.resolvedOptions = function(...args) {
    return {...originalResolvedOptions.apply(this, args), locale: profile.locale, timeZone: profile.timezone_id};
};

const knownFonts = new Set(profile.font_families.map((font) => font.toLowerCase()));
const normalizeFontFamily = (font) => String(font || '')
    .split(',')
    .map((item) => item.trim().replace(/^['"]|['"]$/g, '').toLowerCase())
    .filter(Boolean);
if (document.fonts?.check) {
    const originalFontCheck = document.fonts.check.bind(document.fonts);
    document.fonts.check = (font, text) => {
        const families = normalizeFontFamily(String(font).replace(/^\s*\d+(\.\d+)?(px|pt|em|rem)\s+/i, ''));
        if (families.some((family) => knownFonts.has(family))) return true;
        return originalFontCheck(font, text);
    };
}
if (window.queryLocalFonts) {
    window.queryLocalFonts = async (options = {}) => {
        const requested = new Set((options.postscriptNames || []).map((name) => String(name).toLowerCase()));
        return profile.font_families
            .map((family) => ({
                family,
                fullName: family,
                postscriptName: family.replace(/\s+/g, ''),
                style: 'Regular',
            }))
            .filter((font) => requested.size === 0 || requested.has(font.postscriptName.toLowerCase()));
    };
}

const brands = Object.freeze([
    Object.freeze({brand: 'Chromium', version: String(profile.chrome_major)}),
    Object.freeze({brand: 'Google Chrome', version: String(profile.chrome_major)}),
    Object.freeze({brand: 'Not_A Brand', version: '24'}),
]);
const userAgentData = Object.freeze({
    brands,
    mobile: profile.mobile,
    platform: profile.platform,
    getHighEntropyValues: async (hints) => {
        const values = {
            architecture: profile.architecture,
            bitness: profile.bitness,
            brands,
            fullVersionList: brands.map((item) => ({
                brand: item.brand,
                version: item.brand === 'Not_A Brand' ? '24.0.0.0' : profile.chrome_version,
            })),
            mobile: profile.mobile,
            model: profile.model,
            platform: profile.platform,
            platformVersion: profile.platform_version,
            uaFullVersion: profile.chrome_version,
            wow64: false,
        };
        return Object.fromEntries((hints || []).filter((hint) => hint in values).map((hint) => [hint, values[hint]]));
    },
    toJSON: () => ({brands, mobile: profile.mobile, platform: profile.platform}),
});
defineGetter(Navigator.prototype, 'userAgentData', () => userAgentData);

const makePluginArray = () => {
    const pdfMime = Object.freeze({
        type: 'application/pdf',
        suffixes: 'pdf',
        description: 'Portable Document Format',
        enabledPlugin: null,
    });
    const pdfPlugin = Object.freeze({
        0: pdfMime,
        name: 'Chrome PDF Viewer',
        filename: 'internal-pdf-viewer',
        description: 'Portable Document Format',
        length: 1,
        item: (index) => index === 0 ? pdfMime : null,
        namedItem: (name) => name === pdfMime.type ? pdfMime : null,
    });
    const plugins = Object.freeze({
        0: pdfPlugin,
        length: 1,
        item: (index) => index === 0 ? pdfPlugin : null,
        namedItem: (name) => name === pdfPlugin.name ? pdfPlugin : null,
        refresh: () => undefined,
    });
    const mimeTypes = Object.freeze({
        0: pdfMime,
        length: 1,
        item: (index) => index === 0 ? pdfMime : null,
        namedItem: (name) => name === pdfMime.type ? pdfMime : null,
    });
    return {plugins, mimeTypes};
};
const pluginData = makePluginArray();
defineGetter(Navigator.prototype, 'plugins', () => pluginData.plugins);
defineGetter(Navigator.prototype, 'mimeTypes', () => pluginData.mimeTypes);

const chrome = window.chrome || {};
const runtime = chrome.runtime || {};
Object.assign(runtime, {
    id: profile.runtime_id,
    getManifest: () => ({
        manifest_version: 3,
        name: 'Chrome PDF Viewer',
        version: profile.chrome_version,
    }),
    sendMessage: (...args) => {
        const callback = args.find((arg) => typeof arg === 'function');
        if (callback) queueMicrotask(() => callback(undefined));
        return resolved(undefined);
    },
});
Object.assign(chrome, {runtime});
try { Object.defineProperty(window, 'chrome', {value: chrome, configurable: true}); } catch (_) {}

const installApi = (name, methods) => {
    const api = Object.freeze(methods);
    defineGetter(Navigator.prototype, name, () => api);
};
if (!navigator.bluetooth) {
    installApi('bluetooth', {
        getAvailability: () => resolved(false),
        getDevices: () => resolved([]),
        requestDevice: () => rejected('NotFoundError', 'No Bluetooth device selected.'),
    });
}
if (!navigator.usb) {
    installApi('usb', {
        getDevices: () => resolved([]),
        requestDevice: () => rejected('NotFoundError', 'No USB device selected.'),
    });
}
if (!navigator.hid) {
    installApi('hid', {
        getDevices: () => resolved([]),
        requestDevice: () => resolved([]),
    });
}
if (!navigator.serial) {
    installApi('serial', {
        getPorts: () => resolved([]),
        requestPort: () => rejected('NotFoundError', 'No serial port selected.'),
    });
}
if (!navigator.keyboard) {
    installApi('keyboard', {
        lock: () => resolved(undefined),
        unlock: () => undefined,
        getLayoutMap: () => resolved(new Map()),
    });
}
if (navigator.mediaDevices) {
    const originalEnumerateDevices = navigator.mediaDevices.enumerateDevices?.bind(navigator.mediaDevices);
    navigator.mediaDevices.enumerateDevices = async () => {
        const devices = originalEnumerateDevices ? await originalEnumerateDevices() : [];
        return devices.map((device, index) => ({
            kind: device.kind,
            deviceId: `default-${profile.canvas_seed}-${index}`,
            groupId: `default-group-${profile.canvas_seed}-${index}`,
            label: '',
            toJSON: () => ({
                kind: device.kind,
                deviceId: `default-${profile.canvas_seed}-${index}`,
                groupId: `default-group-${profile.canvas_seed}-${index}`,
                label: '',
            }),
        }));
    };
}
if (navigator.getBattery) {
    defineGetter(Navigator.prototype, 'getBattery', () => undefined);
}
if (navigator.permissions?.query) {
    const originalQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = async (descriptor) => {
        const name = descriptor?.name;
        if (['geolocation', 'notifications', 'camera', 'microphone', 'clipboard-read', 'clipboard-write'].includes(name)) {
            const state = name === 'geolocation' ? 'granted' : 'prompt';
            return Object.freeze({state, onchange: null, name});
        }
        return originalQuery(descriptor);
    };
}
if (navigator.storage?.estimate) {
    navigator.storage.estimate = () => resolved({
        quota: profile.storage_quota,
        usage: profile.storage_usage,
        usageDetails: {indexedDB: Math.floor(profile.storage_usage * 0.55), caches: Math.floor(profile.storage_usage * 0.35)},
    });
}
const connection = Object.freeze({
    effectiveType: profile.connection_effective_type,
    rtt: profile.connection_rtt,
    downlink: profile.connection_downlink,
    saveData: false,
    onchange: null,
});
defineGetter(Navigator.prototype, 'connection', () => connection);
defineGetter(Navigator.prototype, 'mozConnection', () => connection);
defineGetter(Navigator.prototype, 'webkitConnection', () => connection);
if (window.speechSynthesis) {
    try {
        window.speechSynthesis.getVoices = () => [];
    } catch (_) {}
}
if (window.RTCPeerConnection) {
    const NativePeerConnection = window.RTCPeerConnection;
    window.RTCPeerConnection = function(...args) {
        const peer = new NativePeerConnection(...args);
        const nativeAddEventListener = peer.addEventListener.bind(peer);
        peer.addEventListener = (type, listener, options) => {
            if (type !== 'icecandidate' || typeof listener !== 'function') {
                return nativeAddEventListener(type, listener, options);
            }
            return nativeAddEventListener(type, (event) => {
                if (!event.candidate || !/\b(host|srflx)\b/i.test(event.candidate.candidate || '')) {
                    listener(event);
                }
            }, options);
        };
        return peer;
    };
    window.RTCPeerConnection.prototype = NativePeerConnection.prototype;
}

const patchWebGL = (Context) => {
    if (!Context) return;
    const original = Context.prototype.getParameter;
    Context.prototype.getParameter = function(parameter) {
        if (parameter === 37445) return profile.webgl_vendor;
        if (parameter === 37446) return profile.webgl_renderer;
        return original.call(this, parameter);
    };
};
patchWebGL(window.WebGLRenderingContext);
patchWebGL(window.WebGL2RenderingContext);

if (navigator.gpu?.requestAdapter) {
    const originalRequestAdapter = navigator.gpu.requestAdapter.bind(navigator.gpu);
    navigator.gpu.requestAdapter = async (...args) => {
        const adapter = await originalRequestAdapter(...args);
        if (!adapter) return adapter;
        try {
            Object.defineProperty(adapter, 'name', {get: () => profile.webgl_renderer, configurable: true});
            Object.defineProperty(adapter, 'vendor', {get: () => profile.webgl_vendor, configurable: true});
        } catch (_) {}
        return adapter;
    };
}

const addNoise = (imageData) => {
    const data = imageData.data;
    const stride = Math.max(4, Math.floor(data.length / 64 / 4) * 4);
    for (let i = profile.canvas_seed % stride; i < data.length; i += stride) {
        data[i] = Math.max(0, Math.min(255, data[i] + ((profile.canvas_seed + i) % 2 ? 1 : -1)));
    }
    return imageData;
};
const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;
CanvasRenderingContext2D.prototype.getImageData = function(...args) {
    return addNoise(originalGetImageData.apply(this, args));
};
const noisyClone = (canvas) => {
    const clone = document.createElement('canvas');
    clone.width = canvas.width;
    clone.height = canvas.height;
    const context = clone.getContext('2d');
    context.drawImage(canvas, 0, 0);
    if (clone.width && clone.height) {
        const image = originalGetImageData.call(context, 0, 0, clone.width, clone.height);
        context.putImageData(addNoise(image), 0, 0);
    }
    return clone;
};
const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(...args) {
    return originalToDataURL.apply(noisyClone(this), args);
};
const originalToBlob = HTMLCanvasElement.prototype.toBlob;
HTMLCanvasElement.prototype.toBlob = function(...args) {
    return originalToBlob.apply(noisyClone(this), args);
};

const patchAudioContext = (Context) => {
    if (!Context || !Context.prototype) return;
    const originalCreateAnalyser = Context.prototype.createAnalyser;
    if (originalCreateAnalyser) {
        Context.prototype.createAnalyser = function(...args) {
            const analyser = originalCreateAnalyser.apply(this, args);
            const originalGetFloatFrequencyData = analyser.getFloatFrequencyData?.bind(analyser);
            if (originalGetFloatFrequencyData) {
                analyser.getFloatFrequencyData = (array) => {
                    originalGetFloatFrequencyData(array);
                    for (let i = 0; i < array.length; i += 17) array[i] += ((profile.canvas_seed + i) % 3 - 1) * 0.0001;
                };
            }
            return analyser;
        };
    }
};
patchAudioContext(window.AudioContext);
patchAudioContext(window.webkitAudioContext);

const patchRects = (prototype) => {
    if (!prototype) return;
    const original = prototype.getBoundingClientRect;
    if (!original) return;
    prototype.getBoundingClientRect = function(...args) {
        const rect = original.apply(this, args);
        const delta = ((profile.canvas_seed % 7) - 3) / 1000;
        return new DOMRect(
            rect.x + delta,
            rect.y + delta,
            rect.width,
            rect.height
        );
    };
};
patchRects(Element.prototype);
"""

STEALTH_SCRIPT = build_stealth_script()


def cdp_screen_metrics(profile: Profile = DEFAULT_PROFILE) -> dict[str, Any]:
    """Return CDP device metrics matching the injected screen profile."""
    return {
        "width": profile.width,
        "height": profile.height,
        "deviceScaleFactor": profile.device_scale_factor,
        "mobile": profile.mobile,
        "screenWidth": profile.width,
        "screenHeight": profile.height,
        "positionX": 0,
        "positionY": 0,
        "dontSetVisibleSize": False,
        "screenOrientation": {"type": "landscapePrimary", "angle": 0},
    }


def cdp_user_agent_override(profile: Profile = DEFAULT_PROFILE) -> dict[str, Any]:
    """Return CDP UA and UA-CH metadata matching the injected profile."""
    version = profile.chrome_version
    return {
        "userAgent": profile.user_agent,
        "acceptLanguage": ",".join(
            language if index == 0 else f"{language};q={max(0.1, 1 - index / 10):.1f}"
            for index, language in enumerate(profile.languages)
        ),
        "platform": "Win32",
        "userAgentMetadata": {
            "brands": [
                {"brand": "Chromium", "version": str(profile.chrome_major)},
                {"brand": "Google Chrome", "version": str(profile.chrome_major)},
                {"brand": "Not_A Brand", "version": "24"},
            ],
            "fullVersionList": [
                {"brand": "Chromium", "version": version},
                {"brand": "Google Chrome", "version": version},
                {"brand": "Not_A Brand", "version": "24.0.0.0"},
            ],
            "fullVersion": version,
            "platform": profile.platform,
            "platformVersion": profile.platform_version,
            "architecture": profile.architecture,
            "model": profile.model,
            "mobile": profile.mobile,
            "bitness": profile.bitness,
            "wow64": False,
        },
    }


def cdp_geolocation_override(profile: Profile = DEFAULT_PROFILE) -> dict[str, Any]:
    """Return CDP geolocation coordinates matching the context profile."""
    return {
        "latitude": profile.geolocation_latitude,
        "longitude": profile.geolocation_longitude,
        "accuracy": profile.geolocation_accuracy,
    }


def _cdp_commands(profile: Profile) -> list[tuple[str, dict[str, Any]]]:
    return [
        ("Emulation.setDeviceMetricsOverride", cdp_screen_metrics(profile)),
        ("Emulation.setTimezoneOverride", {"timezoneId": profile.timezone_id}),
        ("Emulation.setGeolocationOverride", cdp_geolocation_override(profile)),
        ("Network.setUserAgentOverride", cdp_user_agent_override(profile)),
    ]


def launch_context_options(profile: Profile = DEFAULT_PROFILE) -> dict[str, Any]:
    """Return native Playwright context options that align with the profile."""
    return {
        "locale": profile.locale,
        "timezone_id": profile.timezone_id,
        "geolocation": {
            "latitude": profile.geolocation_latitude,
            "longitude": profile.geolocation_longitude,
            "accuracy": profile.geolocation_accuracy,
        },
        "permissions": ["geolocation"],
        "extra_http_headers": stealth_http_headers(profile),
        "color_scheme": "light",
        "reduced_motion": "no-preference",
    }


def _is_private_network_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            return True
        address = ipaddress.ip_address(host)
        return (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )
    except ValueError:
        return False


def _install_port_scan_protection(page: Any, profile: Profile) -> Any:
    if not profile.port_scan_protection:
        return page

    def sync_route_handler(route: Any) -> Any:
        request = route.request
        if _is_private_network_url(request.url):
            return route.abort()
        return route.continue_()

    async def async_route_handler(route: Any) -> Any:
        request = route.request
        if _is_private_network_url(request.url):
            await route.abort()
        else:
            await route.continue_()

    page_module = type(page).__module__
    route_handler = async_route_handler if "async_api" in page_module else sync_route_handler
    result = page.route("**/*", route_handler)
    if not inspect.isawaitable(result):
        return page

    async def finish_route() -> Any:
        await result
        return page

    return finish_route()


def _install_init_script(target: Any, profile: Profile) -> Any:
    result = target.add_init_script(build_stealth_script(profile))
    if not inspect.isawaitable(result):
        return target

    async def finish_install() -> Any:
        await result
        return target

    return finish_install()


def _apply_cdp_to_page(page: Any, profile: Profile) -> Any:
    session_result = page.context.new_cdp_session(page)
    if not inspect.isawaitable(session_result):
        for method, params in _cdp_commands(profile):
            session_result.send(method, params)
        return page

    async def finish_cdp() -> Any:
        session = await session_result
        for method, params in _cdp_commands(profile):
            await session.send(method, params)
        return page

    return finish_cdp()


def apply_stealth_to_context(context: Any, profile: Profile = DEFAULT_PROFILE) -> Any:
    """Install the profile script and return the context (await for async API)."""
    return _install_init_script(context, profile)


def apply_stealth_to_page(page: Any, profile: Profile = DEFAULT_PROFILE) -> Any:
    """Install the profile script and matching CDP screen metrics on a page."""
    script_result = _install_init_script(page, profile)
    if not inspect.isawaitable(script_result):
        route_result = _install_port_scan_protection(page, profile)
        if inspect.isawaitable(route_result):
            async def finish_sync_route() -> Any:
                await route_result
                return _apply_cdp_to_page(page, profile)
            return finish_sync_route()
        return _apply_cdp_to_page(page, profile)

    async def finish_page() -> Any:
        await script_result
        route_result = _install_port_scan_protection(page, profile)
        if inspect.isawaitable(route_result):
            await route_result
        return await _apply_cdp_to_page(page, profile)

    return finish_page()


def stealth_http_headers(profile: Profile = DEFAULT_PROFILE) -> dict[str, str]:
    """Return headers aligned with the generated browser profile."""
    return {
        "Accept-Language": ",".join(
            language if index == 0 else f"{language};q={max(0.1, 1 - index / 10):.1f}"
            for index, language in enumerate(profile.languages)
        )
    }
