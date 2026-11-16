#!/usr/bin/env python3
"""Fetch many URLs through fetch_page.py and report what actually worked.

The egress is chosen per URL, not globally: some sites only answer over the
proxy, some only when the proxy is off, and a site can be on the wrong side of
that line without any visible error. With --route auto (the default) each URL
is probed over both paths and the browser is run on the path that answered,
falling back to the other one once.

    python scripts/batch_fetch.py urls.txt
    python scripts/batch_fetch.py urls.txt -j 3 --headed --wait 90
    python scripts/batch_fetch.py urls.txt --route direct      # proxy must be off
    python scripts/batch_fetch.py --url https://a.example --url https://b.example

Outputs, under --out (default ./fetch-out/<timestamp>):

    index.md       human-readable pass/fail table, including the egress used
    results.jsonl  one JSON record per URL
    corpus.md      concatenated successes, ready to feed a model
    pages/         per-URL .txt + .html
    .profiles/     per-job browser profiles (deleted unless --keep-profiles)

Exit code 0 when every URL succeeded, 1 when some did not.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
FETCH = HERE / "fetch_page.py"
sys.path.insert(0, str(HERE))

try:  # same directory; importing it does not import Playwright
    from fetch_page import detect_proxy  # type: ignore
except Exception:  # pragma: no cover - the skill still works without it
    def detect_proxy() -> str | None:
        return None

PROBE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

STATUS_ICON = {
    "ok": "OK",
    "short": "SHORT",
    "blocked-datadome": "DATADOME",
    "blocked-perimeterx": "PERIMETERX",
    "blocked-akamai": "AKAMAI",
    "blocked-cloudflare": "CLOUDFLARE",
    "blocked-challenge": "CHALLENGE",
    "blocked": "BLOCKED",
    "timeout": "TIMEOUT",
    "error": "ERROR",
}

HANDOFF = {"blocked-datadome", "blocked-perimeterx", "blocked-akamai"}


def resolve_python() -> tuple[list[str], str]:
    """Return the interpreter prefix for fetch_page.py plus a label."""
    venv = SKILL / ".venv" / "bin" / "python"
    if venv.exists():
        return [str(venv)], f"venv {venv}"
    try:
        import playwright  # noqa: F401
        return [sys.executable], f"system python {sys.executable}"
    except ImportError:
        pass
    uv = shutil.which("uv") or shutil.which("uvx")
    if uv:
        return [uv, "run", "--with", "playwright", "--python", "3.12", "python"], "uv (cached env)"
    return [sys.executable], "no playwright (install it or uv)"


def slugify(url: str) -> str:
    host_path = re.sub(r"^[a-z]+://", "", url)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", host_path).strip("-").lower()
    return (slug[:80] or "page").rstrip("-")


def parse_report(stdout: str) -> dict | None:
    index = stdout.find("{")
    if index < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(stdout[index:])
        return payload if isinstance(payload, dict) else None
    except ValueError:
        return None


def classify(report: dict | None, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if report is None:
        return "error"
    if report.get("ok"):
        return "ok"
    if report.get("stage") in ("browser_start", "playwright"):
        return "error"
    blocker = report.get("blocker")
    if blocker:
        return f"blocked-{blocker}"
    if not report.get("challenge_cleared"):
        return "blocked-challenge"
    return "short"


def note_for(report: dict | None, status: str, err: str) -> str:
    if status == "ok":
        return ""
    if status == "timeout":
        return "exceeded the per-URL budget; raise --timeout or --wait"
    if status == "error":
        if report:
            return str(report.get("error", ""))[:160]
        return (err.strip().splitlines()[-1] if err.strip() else "no JSON report")[:160]
    if status in HANDOFF:
        return "residential IP or human handoff only; do not retry here"
    if status == "blocked-cloudflare":
        return "retry with --headed --wait 90, or switch egress"
    if status == "blocked-challenge":
        return "retry with --headed, or switch egress"
    if status == "short":
        return "loaded but too little text; page may be a shell or login wall"
    return ""


def probe(url: str, proxy: str | None, timeout: float = 8.0,
          min_bytes: int = 200) -> dict:
    """Cheap reachability check over one egress. A hint, not a verdict:
    urllib has a different TLS fingerprint than Chromium, so a 403 here can
    still open in the browser. A success is a strong signal; a failure is not.
    """
    if proxy and "://" in proxy and proxy.split("://", 1)[0].lower().startswith("socks"):
        # urllib cannot speak SOCKS without a plugin; the browser can, so let
        # the browser decide instead of marking this route dead.
        return {"ok": None, "status": None, "bytes": 0, "error": "socks"}
    proxy_map = ({"http": f"http://{proxy}", "https": f"http://{proxy}"}
                 if proxy else {})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxy_map),  # {} also disables env proxies
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
    )
    request = urllib.request.Request(url, headers={
        "User-Agent": PROBE_UA,
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(131072)
            return {"ok": response.status < 400 and len(body) >= min_bytes,
                    "status": response.status, "bytes": len(body)}
    except Exception as exc:
        return {"ok": False, "status": None, "bytes": 0, "error": type(exc).__name__}


def resolve_proxy_for(url: str, cfg: argparse.Namespace) -> str | None:
    """The proxy to use for one URL. In auto mode this is per host, because a
    PAC-based setup proxies some hosts and sends the rest direct."""
    if cfg.proxy_mode == "fixed":
        return cfg.proxy_value
    if cfg.proxy_mode == "none":
        return None
    return detect_proxy(urlparse(url).hostname)


def route_candidates(url: str, cfg: argparse.Namespace) -> list[tuple[str, str | None]]:
    """Ordered (route_name, proxy_or_None) pairs to try for this URL."""
    candidates: list[tuple[str, str | None]] = [("direct", None)]
    proxy = resolve_proxy_for(url, cfg)
    if proxy:
        candidates.append(("proxy", proxy))
    if cfg.route == "direct":
        return candidates[:1]
    if cfg.route == "proxy":
        return candidates[-1:]
    if len(candidates) == 1:
        return candidates
    scored = [(name, proxy, probe(url, proxy, cfg.probe_timeout))
              for name, proxy in candidates]
    # answered > cannot tell (SOCKS) > failed; "direct" wins ties.
    rank = {True: 0, None: 1, False: 2}
    ordered = sorted(scored, key=lambda item: (rank[item[2]["ok"]],
                                               0 if item[0] == "direct" else 1))
    if cfg.verbose:
        detail = ", ".join(
            f"{n}:{'ok' if r['ok'] is True else ('?' if r['ok'] is None else (r.get('error') or 'fail'))}"
            for n, _, r in scored)
        print(f"      probe {url} [{detail}]", flush=True)
    return [(name, proxy) for name, proxy, _ in ordered]


def run_attempt(index: int, url: str, cfg: argparse.Namespace, python_prefix: list[str],
                route: str, proxy: str | None, attempt: int) -> dict:
    slug = f"{index:02d}-{slugify(url)}"
    if attempt:
        slug = f"{slug}.{route}"
    prefix = cfg.out / "pages" / slug
    prefix.parent.mkdir(parents=True, exist_ok=True)
    args = [
        str(FETCH), url, str(prefix),
        "--wait", str(cfg.wait),
        "--port", str(cfg.port_base + index),
        "--profile", str(cfg.out / ".profiles" / f"job-{index:02d}"),
        "--proxy", proxy or "none",
        "--min-text", str(cfg.min_text),
    ]
    if cfg.headed:
        args.append("--headed")
    if cfg.insecure:
        args.append("--insecure")

    started = time.time()
    timed_out = False
    stdout = stderr = ""
    try:
        proc = subprocess.run(python_prefix + args, capture_output=True,
                              text=True, timeout=cfg.timeout)
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")

    report = parse_report(stdout)
    status = classify(report, timed_out)
    return {
        "index": index,
        "url": url,
        "status": status,
        "ok": status == "ok",
        "chars": (report or {}).get("text_chars", 0),
        "title": (report or {}).get("title", ""),
        "final_url": (report or {}).get("final_url", url),
        "blocker": (report or {}).get("blocker"),
        "route": route,
        "egress": proxy or "direct",
        "headed": bool(cfg.headed),
        "seconds": round(time.time() - started, 1),
        "saved": (report or {}).get("saved", []),
        "note": note_for(report, status, stderr),
    }


def run_one(index: int, url: str, cfg: argparse.Namespace, python_prefix: list[str]) -> dict:
    attempts: list[dict] = []
    for attempt_no, (route, proxy) in enumerate(route_candidates(url, cfg)):
        record = run_attempt(index, url, cfg, python_prefix, route, proxy, attempt_no)
        attempts.append(record)
        if record["ok"]:
            break
    # Report the best-effort result: most text wins, otherwise the last try.
    best = max(attempts, key=lambda r: (r["ok"], r["chars"]))
    best["tried"] = [{"route": a["route"], "status": a["status"], "chars": a["chars"]}
                     for a in attempts]
    if len(attempts) > 1:
        best["note"] = (best["note"] + " | tried " +
                        " then ".join(a["route"] for a in attempts)).strip(" |")
    icon = STATUS_ICON.get(best["status"], best["status"].upper())
    print(f"[{index:02d}] {icon:<12} {best['chars']:>7} chars "
          f"{best['seconds']:>6.1f}s  {best['route']:<6} {url}", flush=True)
    return best


def write_outputs(records: list[dict], cfg: argparse.Namespace) -> None:
    records = sorted(records, key=lambda r: r["index"])
    (cfg.out / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")

    wins = sum(1 for r in records if r["ok"])
    lines = [
        f"# Batch fetch — {wins}/{len(records)} succeeded",
        "",
        f"- run: `{cfg.out}`",
        f"- route policy: `{cfg.route}`; proxy `{cfg.proxy_value}`"
        if cfg.proxy_mode == "fixed" else
        f"- route policy: `{cfg.route}`; proxy {'off' if cfg.proxy_mode == 'none' else 'auto per host'}",
        f"- mode: {'headed' if cfg.headed else 'headless'}",
        "",
        "| # | status | route | chars | title | url | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in records:
        title = (r["title"] or "-").replace("|", "\\|")[:60]
        note = (r["note"] or "").replace("|", "\\|")[:70]
        lines.append(f"| {r['index']:02d} | {r['status']} | {r['route']} | {r['chars']} "
                     f"| {title} | {r['url']} | {note} |")
    (cfg.out / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    parts = []
    for r in records:
        if not r["ok"]:
            continue
        txt = next((p for p in r["saved"] if p.endswith(".txt")), None)
        if not txt or not Path(txt).exists():
            continue
        body = Path(txt).read_text(encoding="utf-8", errors="replace").strip()
        if not body:
            continue
        parts.append(f"## {r['title'] or r['url']}\n\nSource: {r['final_url']}\n\n{body}\n")
    (cfg.out / "corpus.md").write_text("\n\n---\n\n".join(parts), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls_file", nargs="?", help="text file, one URL per line (# comments ok)")
    ap.add_argument("--url", action="append", default=[], help="URL; repeatable")
    ap.add_argument("-j", "--jobs", type=int, default=3, help="parallel browsers (default 3)")
    ap.add_argument("--wait", type=int, default=60, help="challenge wait per URL (default 60)")
    ap.add_argument("--timeout", type=int, default=300, help="hard budget per attempt (default 300)")
    ap.add_argument("--min-text", type=int, default=400)
    ap.add_argument("--headed", action="store_true", help="real windows; needed by many WAFs")
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--route", default="auto", choices=("auto", "direct", "proxy"),
                    help="auto probes both egresses per URL (default)")
    ap.add_argument("--proxy", default="auto",
                    help="auto (system/PAC) | none | host:port")
    ap.add_argument("--probe-timeout", type=float, default=8.0)
    ap.add_argument("--port-base", type=int, default=9400)
    ap.add_argument("--out", default=None)
    ap.add_argument("--keep-profiles", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="show the plan and exit")
    cfg = ap.parse_args(argv)

    urls: list[str] = list(cfg.url)
    if cfg.urls_file:
        text = Path(cfg.urls_file).read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if not urls:
        ap.error("no URLs: pass a file or --url")

    seen: set[str] = set()
    unique = [u for u in urls if not (u in seen or seen.add(u))]

    if cfg.proxy in ("auto", ""):
        cfg.proxy_mode, cfg.proxy_value = "auto", None
    elif cfg.proxy == "none":
        cfg.proxy_mode, cfg.proxy_value = "none", None
    else:
        cfg.proxy_mode, cfg.proxy_value = "fixed", cfg.proxy

    cfg.out = Path(cfg.out or f"fetch-out/{time.strftime('%Y%m%d-%H%M%S')}").resolve()
    cfg.out.mkdir(parents=True, exist_ok=True)

    python_prefix, label = resolve_python()
    print(f"runner : {label}", flush=True)
    print(f"out    : {cfg.out}", flush=True)
    proxy_label = {"auto": "auto (per host/PAC)", "none": "off",
                   "fixed": cfg.proxy_value}[cfg.proxy_mode]
    print(f"proxy  : {proxy_label}   route: {cfg.route}", flush=True)
    print(f"jobs   : {cfg.jobs}  urls: {len(unique)}", flush=True)
    if cfg.dry_run:
        for i, url in enumerate(unique):
            print(f"  [{i:02d}] {url}  port={cfg.port_base + i}")
        return 0
    if not FETCH.exists():
        print(f"error: fetch_page.py not found at {FETCH}", file=sys.stderr)
        return 2

    started = time.time()
    records: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, cfg.jobs)) as pool:
        futures = {pool.submit(run_one, i, url, cfg, python_prefix): url
                   for i, url in enumerate(unique)}
        for future in concurrent.futures.as_completed(futures):
            records.append(future.result())

    write_outputs(records, cfg)
    if not cfg.keep_profiles:
        shutil.rmtree(cfg.out / ".profiles", ignore_errors=True)

    wins = sum(1 for r in records if r["ok"])
    print(f"\n{wins}/{len(records)} ok in {time.time() - started:.0f}s -> {cfg.out / 'index.md'}")
    return 0 if wins == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
