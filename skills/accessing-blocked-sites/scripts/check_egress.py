#!/usr/bin/env python3
"""Check whether the direct and proxied paths actually reach the internet.

A proxy port that accepts TCP can still be dead upstream: HTTPS gets
`CONNECT 200` and the TLS handshake is dropped, or plain HTTP returns 502.
That state looks exactly like "the site is blocked", so test the egress
before drawing conclusions about a target.

    python scripts/check_egress.py
    python scripts/check_egress.py --proxy <host:port>
    python scripts/check_egress.py --proxy none

Exit code 0 when at least one path works, 1 when neither does.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from batch_fetch import probe  # noqa: E402
from fetch_page import detect_proxy  # noqa: E402

TARGETS = (
    "https://example.com",
    "https://www.google.com",
    "https://www.cloudflare.com",
)


def fmt(result: dict | None) -> str:
    if result is None:
        return "-"
    if result.get("status"):
        return f"{result['status']} {result['bytes']}B"
    return f"ERR {result.get('error', 'fail')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--proxy", default="auto", help="auto | none | host:port")
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)

    if args.proxy == "auto":
        proxy = detect_proxy()
    elif args.proxy == "none":
        proxy = None
    else:
        proxy = args.proxy

    print(f"direct : no proxy")
    print(f"proxy  : {proxy or 'off'}\n")
    print(f"{'target':<34}{'direct':<18}{'proxy':<18}")
    rows: list[tuple[str, dict, dict | None]] = []
    for target in TARGETS:
        direct = probe(target, None, args.timeout)
        proxied = probe(target, proxy, args.timeout) if proxy else None
        rows.append((target, direct, proxied))
        print(f"{target:<34}{fmt(direct):<18}{fmt(proxied):<18}")

    direct_ok = sum(1 for row in rows if row[1]["ok"])
    proxy_ok = sum(1 for row in rows if row[2] and row[2]["ok"])

    print()
    if not proxy:
        verdict = ("proxy off; direct path answers %d/%d (blocked-here domains will fail)"
                   % (direct_ok, len(rows)))
    elif direct_ok == 0 and proxy_ok == 0:
        verdict = "neither path works: no usable internet from this machine"
    elif proxy_ok == 0:
        verdict = ("proxy accepts connections but cannot carry traffic "
                   "(502 or dropped TLS) — fix the proxy client, not the site")
    elif direct_ok == 0:
        verdict = "proxy only: direct is blocked from this network"
    else:
        verdict = (f"both paths partly work (direct {direct_ok}/{len(rows)}, "
                   f"proxy {proxy_ok}/{len(rows)}); a target still failing is "
                   f"about that target's egress, not the machine")
    print(f"verdict: {verdict}")

    if proxy and proxy_ok == 0 and any(
            row[2] and row[2].get("status") == 502 for row in rows):
        print("hint: HTTP 502 through a CONNECT-capable port means the upstream/node is down")
    return 0 if (direct_ok or proxy_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
