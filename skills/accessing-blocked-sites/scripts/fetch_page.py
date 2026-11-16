"""Fetch a page that plain HTTP clients cannot read.

Renders the URL in a real Chromium-family browser (Edge, Chrome or Chromium)
driven over CDP: Cloudflare and JS-rendered sites only yield text to a real
browser.

Behaviour:
  * a fresh browser per run, so --headed is never swallowed by a leftover
    headless instance;
  * the UA is rebuilt from the real browser version, because headless
    Chromium advertises "HeadlessChrome" and is fingerprinted;
  * the challenge is polled until markers vanish and the text is substantial,
    then re-judged on the captured page;
  * the proxy comes from the OS (WinINET, scutil/PAC, *_proxy) unless given.

Usage:
    python scripts/fetch_page.py <url> [out_prefix]
        [--proxy auto|none|<host:port>]   default auto (system proxy)
        [--headed]                        real window (needed for Cloudflare)
        [--wait 60]                       max seconds to wait out a challenge
        [--browser auto|edge|chrome|chromium]
        [--browser-path PATH]             explicit executable
        [--profile DIR]                   dedicated profile (never your own)
        [--port 9335]                     CDP port
        [--keep]                          leave the browser running
        [--reuse]                         reuse an already-running CDP browser
        [--html-only]                     skip the .txt sidecar
        [--insecure]                      ignore certificate errors
        [--no-sandbox]                    containers/root only; reduces isolation
        [--ua STRING]                     override the user agent

Exit code 0 only when the challenge cleared and text looks substantial.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")

CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "under attack",
    "enable javascript and cookies",
    "verifying you are human",
    "cf-mitigated",
    "请稍候",
    "安全验证",
    "正在检查",
)

# Bot-management vendors that do not yield to a fresh browser on a datacenter
# IP. Detected after the fact so the caller can stop escalating and hand off.
BLOCKER_MARKERS = {
    "datadome": ("captcha-delivery.com", "datadome", "dd_cookie_test", "geo.captcha"),
    "perimeterx": ("px-captcha", "perimeterx.com", "pxhc", "_pxappid"),
    "akamai": ("_abck", "akamai bot manager", "sec-cpt"),
    "cloudflare": ("cf-chl", "challenge-platform", "cf_chl_opt"),
}

# A bot gate is short: a headline and maybe a button. Prose above this length is
# content, so the marker scan below applies only to pages shorter than this.
INTERSTITIAL_MAX_TEXT = 1500

# Vendor references appear in markup attributes, not in running text.
_ATTR_VALUE_RE = re.compile(r'(?:src|href|action|data-[a-z0-9-]+)\s*=\s*["\']([^"\']{1,400})',
                            re.IGNORECASE)


def text_weight(text: str) -> int:
    """Length in Latin-equivalent characters: one CJK character counts as two."""
    cjk = sum(1 for ch in text if ord(ch) >= 0x2E80)
    return len(text) + cjk



def platform_ua() -> str:
    if sys.platform == "win32":
        return "Windows NT 10.0; Win64; x64"
    if sys.platform == "darwin":
        return "Macintosh; Intel Mac OS X 10_15_7"
    return "X11; Linux x86_64"


def browser_candidates():
    """Return [(kind, path)] in preference order."""
    out = []
    if sys.platform == "win32":
        edge = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        chrome = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        out += [("edge", p) for p in edge] + [("chrome", p) for p in chrome]
    elif sys.platform == "darwin":
        home = os.path.expanduser("~")
        out += [
            ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("chrome", f"{home}/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ("edge", "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ("chromium", "/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:
        for kind, names in (
            ("chrome", ("google-chrome", "google-chrome-stable")),
            ("chromium", ("chromium", "chromium-browser")),
            ("edge", ("microsoft-edge", "microsoft-edge-stable")),
        ):
            for name in names:
                found = shutil.which(name)
                if found:
                    out.append((kind, found))
    seen = set()
    return [(k, p) for k, p in out if p and not (p in seen or seen.add(p))]


def find_browser(kind: str = "auto", explicit: str | None = None):
    if explicit:
        if not os.path.exists(explicit):
            raise RuntimeError(f"browser executable not found: {explicit}")
        return "custom", explicit
    for candidate_kind, path in browser_candidates():
        if not os.path.exists(path):
            continue
        if kind == "auto" or kind == candidate_kind:
            return candidate_kind, path
    raise RuntimeError(
        "No Chromium-family browser found. Install Edge/Chrome/Chromium, or pass "
        "--browser-path. On Linux: sudo apt install chromium.")


PROXY_SCHEMES = ("http", "https", "socks5", "socks5h", "socks4", "socks4a")


def proxy_scheme(proxy: str) -> str:
    return proxy.split("://", 1)[0].lower() if "://" in proxy else "http"


def is_socks(proxy: str) -> bool:
    return proxy_scheme(proxy).startswith("socks")


def proxy_hostport(proxy: str) -> tuple[str, int] | None:
    raw = proxy.split("://", 1)[-1]
    match = re.search(r"(\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.\-]+):(\d{2,5})", raw)
    return (match.group(1), int(match.group(2))) if match else None


def clean_proxy(value: str | None) -> str | None:
    """Normalise a proxy setting into `host:port` or `scheme://host:port`.

    Users arrive with wildly different setups: a bare port, an HTTP proxy, a
    SOCKS5 tunnel, or a WinINET string like `http=a:1;https=b:2`. Keep the
    scheme when it is not plain HTTP so Chromium gets something it can use.
    """
    if not value:
        return None
    value = value.strip()
    if "=" in value:  # "http=host:port;https=host:port"
        value = value.split("=", 1)[1].split(";")[0]
    scheme = "http"
    prefixed = re.match(r"^([a-z][a-z0-9+.-]*)://", value, flags=re.I)
    if prefixed and prefixed.group(1).lower() in PROXY_SCHEMES:
        scheme = prefixed.group(1).lower()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value, flags=re.I)
    match = re.search(r"(\[[0-9a-fA-F:]+\]|[A-Za-z0-9_.\-]+):(\d{2,5})", value)
    if not match:
        return None
    hostport = f"{match.group(1)}:{match.group(2)}"
    return hostport if scheme == "http" else f"{scheme}://{hostport}"


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def _proxy_works(proxy: str, timeout: float = 4.0) -> bool:
    """Reject listeners that accept TCP but cannot actually proxy HTTPS.

    A PAC often names more than one endpoint, including split-tunnel helpers
    that only handle one host. Handing those to Chromium breaks every request.
    """
    if is_socks(proxy):
        endpoint = proxy_hostport(proxy)
        return bool(endpoint and _port_open(endpoint[0], endpoint[1]))
    handler = urllib.request.ProxyHandler({"http": f"http://{proxy}",
                                           "https": f"http://{proxy}"})
    # Liveness probe only: no secrets are sent, and a bundled Python often
    # lacks the system CA store, so certificate errors must not count as
    # "proxy down".
    insecure = urllib.request.HTTPSHandler(context=ssl._create_unverified_context())
    try:
        with urllib.request.build_opener(handler, insecure).open("https://example.com",
                                                                 timeout=timeout) as response:
            return response.status < 500
    except Exception:
        return False


_PAC_CACHE: dict[str, str | None] = {}


def _pac_url() -> str | None:
    """Where the OS says its PAC file lives, if anywhere."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                                 text=True, timeout=10).stdout
            match = re.search(r"ProxyAutoConfigURLString\s*:\s*(\S+)", out)
            return match.group(1) if match else None
        except Exception:
            return None
    if sys.platform == "win32":
        try:
            ps = (
                "$k = Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\"
                "CurrentVersion\\Internet Settings'; "
                "if ($k.AutoConfigURL) { $k.AutoConfigURL }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            return out or None
        except Exception:
            return None
    return None


def _pac_text() -> str | None:
    """Read the PAC body once per process."""
    if "body" in _PAC_CACHE:
        return _PAC_CACHE["body"]
    body: str | None = None
    url = _pac_url()
    if url:
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                body = response.read().decode("utf-8", "replace")
        except Exception:
            body = None
    _PAC_CACHE["body"] = body
    return body


def pac_routes_host(pac: str, host: str | None) -> bool:
    """Heuristic: does this PAC send `host` through a proxy?

    A PAC that lists specific domains ("only these hosts") answers DIRECT for
    everything else, so its proxy endpoint must not be used for other sites.
    With no host, or no recognisable domain list, assume the proxy applies.
    """
    if not host:
        return True
    suffixes = set(re.findall(r"""["']\.?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)["']""", pac))
    if not suffixes:
        return True
    host = host.lower()
    return any(host == item.lower() or host.endswith("." + item.lower()) for item in suffixes)


def pac_proxy(host: str | None = None) -> str | None:
    """Resolve a proxy from the OS PAC file when no static proxy is set.

    Split-tunnel tools often publish a PAC without turning on the static HTTP
    proxy. The OS then reports no proxy at all, and a naive reader concludes
    "no proxy" and goes direct while the browser is proxied -- or the other
    way round. Parse the PAC instead. Both macOS (scutil) and Windows
    (AutoConfigURL) are supported; Linux relies on *_proxy variables.
    """
    pac = _pac_text()
    if not pac or not pac_routes_host(pac, host):
        return None
    seen: set[str] = set()
    for proxy_host, port in re.findall(r"(?:PROXY|SOCKS5?)\s+([A-Za-z0-9_.\-]+):(\d{2,5})", pac):
        candidate = f"{proxy_host}:{port}"
        if candidate in seen or not _port_open(proxy_host, int(port)):
            continue
        seen.add(candidate)
        if _proxy_works(candidate):
            return candidate
    return None


def detect_proxy(host: str | None = None) -> str | None:
    """Return the system proxy for `host` (`host:port` or `scheme://host:port`).

    `host` matters only for PAC resolution: a split-tunnel PAC proxies some
    hosts and sends the rest direct. Nothing here assumes a port: the value
    comes from the OS (or `*_proxy`), so any client that configures the system
    proxy works -- Clash, Surge, v2rayN, a corporate PAC, or a remote proxy.
    """
    # Environment first: portable and explicit.
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "ALL_PROXY", "all_proxy"):
        proxy = clean_proxy(os.environ.get(key))
        if proxy:
            return proxy

    if sys.platform == "win32":
        try:
            ps = (
                "$k = Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\"
                "CurrentVersion\\Internet Settings'; "
                "if ($k.ProxyEnable -eq 1 -and $k.ProxyServer) { $k.ProxyServer }"
            )
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            proxy = clean_proxy(out)
            if proxy:
                return proxy
        except Exception:
            pass
        return pac_proxy(host)

    if sys.platform == "darwin":
        try:
            out = subprocess.run(["scutil", "--proxy"], capture_output=True,
                                 text=True, timeout=10).stdout
            for scheme in ("HTTPS", "HTTP"):
                if not re.search(rf"{scheme}Enable\s*:\s*1", out):
                    continue
                proxy_host = re.search(rf"{scheme}Proxy\s*:\s*(\S+)", out)
                proxy_port = re.search(rf"{scheme}Port\s*:\s*(\d+)", out)
                if proxy_host and proxy_port:
                    return f"{proxy_host.group(1)}:{proxy_port.group(1)}"
        except Exception:
            pass
        return pac_proxy(host)

    return None


def cdp_alive(port: int, timeout: int = 2) -> str | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                    timeout=timeout) as response:
            return json.load(response).get("Browser")
    except Exception:
        return None


def _profile_arg(profile: str) -> str:
    return profile.replace("'", "''")


def stop_browser(profile: str, port: int) -> None:
    """Kill only browser processes carrying OUR dedicated profile path."""
    if sys.platform == "win32":
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { ($_.Name -eq 'msedge.exe' -or $_.Name -eq 'chrome.exe' "
            "-or $_.Name -eq 'chromium.exe') -and "
            f"$_.CommandLine -like '*{_profile_arg(profile)}*' -and "
            "$_.CommandLine -notlike '*--type=*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=30)
    else:
        try:
            out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                                 text=True, timeout=10).stdout
            for line in out.splitlines():
                match = re.match(r"\s*(\d+)\s+(.*)", line)
                if not match:
                    continue
                pid, args = int(match.group(1)), match.group(2)
                is_browser = any(name in args.lower() for name in (
                    "chrome", "chromium", "msedge", "edge", "brave", "vivaldi"))
                if (profile in args and "--type=" not in args and is_browser
                        and pid != os.getpid()):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
        except Exception:
            pass
    for _ in range(10):
        if not cdp_alive(port, 1):
            return
        time.sleep(1)


def start_browser(kind: str, path: str, headed: bool, proxy: str | None,
                  fresh: bool, profile: str, port: int,
                  no_sandbox: bool = False) -> None:
    if fresh and cdp_alive(port):
        stop_browser(profile, port)
    if cdp_alive(port):
        return
    os.makedirs(profile, exist_ok=True)
    args = [
        path,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--lang=en-US",
    ]
    if proxy:
        server = proxy if "://" in proxy else f"http://{proxy}"
        args.append(f"--proxy-server={server}")
    else:
        # "none" must mean truly direct: without this flag Chromium still
        # follows the system proxy/PAC, so a "direct" retry would silently
        # reuse the same egress.
        args.append("--no-proxy-server")
    if no_sandbox:
        args.append("--no-sandbox")
    if not headed:
        args += ["--headless=new", "--window-size=1440,1000"]
    args.append("about:blank")
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        if cdp_alive(port, 1):
            return
    raise RuntimeError("browser did not expose CDP within 30s")


def ua_for(kind: str, version: str) -> str:
    major = (version or "131.0.0.0").split(".")[0]
    chrome = f"Chrome/{major}.0.0.0"
    if kind == "edge":
        return (f"Mozilla/5.0 ({platform_ua()}) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) {chrome} Edg/{major}.0.0.0 Safari/537.36")
    return (f"Mozilla/5.0 ({platform_ua()}) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) {chrome} Safari/537.36")


def looks_challenged(title: str, text: str) -> bool:
    """True when the captured page is a bot gate rather than content.

    The title is matched always; the body only below INTERSTITIAL_MAX_TEXT.
    """
    lowered_title = title.lower()
    if any(marker in lowered_title for marker in CHALLENGE_MARKERS):
        return True
    body = text.strip()
    if len(body) >= INTERSTITIAL_MAX_TEXT:
        return False
    return any(marker in body[:2000].lower() for marker in CHALLENGE_MARKERS)


def detect_blocker(title: str, text: str, html: str) -> str | None:
    """Name the bot-management vendor the page's markup references, if any.

    Matched against the title and markup attributes always; the body only below
    INTERSTITIAL_MAX_TEXT.
    """
    refs = " ".join(_ATTR_VALUE_RE.findall(html[:400000])).lower()
    blob = f"{title.lower()} {refs}"
    body = text.strip()
    if len(body) < INTERSTITIAL_MAX_TEXT:
        blob += " " + body[:2000].lower()
    for name, markers in BLOCKER_MARKERS.items():
        if any(marker in blob for marker in markers):
            return name
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("out_prefix", nargs="?", default="page")
    ap.add_argument("--proxy", default="auto",
                    help="auto (system proxy) | none | host:port or http://host:port")
    ap.add_argument("--headed", action="store_true",
                    help="show a real window (required by many Cloudflare sites)")
    ap.add_argument("--wait", type=int, default=60)
    ap.add_argument("--browser", default="auto",
                    choices=("auto", "edge", "chrome", "chromium"))
    ap.add_argument("--browser-path", default=None)
    ap.add_argument("--profile", default=os.path.join(tempfile.gettempdir(),
                                                      "dsh-fetch-profile"))
    ap.add_argument("--port", type=int, default=9335)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--reuse", action="store_true",
                    help="reuse an already-running CDP browser instead of a fresh one")
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--insecure", action="store_true",
                    help="ignore HTTPS certificate errors")
    ap.add_argument("--no-sandbox", action="store_true",
                    help="pass --no-sandbox to Chromium (containers/root only; "
                         "reduces isolation)")
    ap.add_argument("--min-text", type=int, default=400)
    ap.add_argument("--ua", default=None)
    args = ap.parse_args(argv)

    proxy = detect_proxy(urlparse(args.url).hostname) if args.proxy == "auto" else (
        None if args.proxy == "none" else clean_proxy(args.proxy))
    report: dict = {
        "requested": args.url,
        "proxy": proxy,
        "headed": args.headed,
        "profile": args.profile,
        "port": args.port,
    }

    try:
        kind, path = find_browser(args.browser, args.browser_path)
        report["browser"] = {"kind": kind, "path": path}
        start_browser(kind, path, args.headed, proxy, not args.reuse,
                      args.profile, args.port, args.no_sandbox)
    except Exception as exc:
        report |= {"ok": False, "stage": "browser_start", "error": str(exc)}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        report |= {
            "ok": False, "stage": "playwright",
            "error": "playwright is not installed. Run: pip install playwright "
                     "(no `playwright install` browser download is needed; this "
                     "script drives your installed Edge/Chrome).",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}")
        version = browser.version or "131.0.0.0"
        ua = args.ua or ua_for(kind, version)
        try:
            ctx = browser.new_context(
                user_agent=ua, locale="en-US",
                viewport={"width": 1440, "height": 1000},
                ignore_https_errors=args.insecure,
            )
            report["ua_forced"] = True
        except Exception as exc:
            ctx = browser.contexts[0]
            report["ua_forced"] = False
            report["ua_warning"] = f"new_context failed: {exc}"

        page = ctx.new_page()
        page.set_default_timeout(60_000)
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            report["goto_warning"] = str(exc)

        deadline = time.time() + args.wait
        cleared = False
        text = ""
        while time.time() < deadline:
            try:
                title = page.title()
                text = page.evaluate("document.body ? document.body.innerText : ''")
            except Exception:
                title, text = "", ""
            if not looks_challenged(title, text) and len(text.strip()) >= args.min_text:
                cleared = True
                break
            time.sleep(3)

        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:
            pass

        final_url = page.url
        title = page.title()
        text = page.evaluate("document.body ? document.body.innerText : ''")
        html = page.content()

        # A challenge can clear during the networkidle wait, after the polling
        # deadline. Judge the page that was actually captured, not the loop.
        cleared = (not looks_challenged(title, text)
                   and text_weight(text.strip()) >= args.min_text)

        written = []
        prefix = Path(args.out_prefix)
        prefix.parent.mkdir(parents=True, exist_ok=True)
        if not args.html_only:
            txt_path = Path(str(prefix) + ".txt")
            txt_path.write_text(text, encoding="utf-8")
            written.append(str(txt_path.resolve()))
        html_path = Path(str(prefix) + ".html")
        html_path.write_text(html, encoding="utf-8")
        written.append(str(html_path.resolve()))

        report |= {
            "ok": cleared,
            "rung": "browser-cdp",
            "final_url": final_url,
            "title": title,
            "challenge_cleared": cleared,
            "blocker": detect_blocker(title, text, html),
            "text_chars": len(text),
            "text_weight": text_weight(text),
            "html_chars": len(html),
            "cookies": len(ctx.cookies()),
            "saved": written,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("\n--- first 1200 chars ---")
        print(text[:1200])

        if not args.keep:
            try:
                page.close()
            except Exception:
                pass

    if not args.keep:
        stop_browser(args.profile, args.port)
    return 0 if cleared else 1


if __name__ == "__main__":
    sys.exit(main())
