---
name: accessing-blocked-sites
description: Use when a page cannot be read from the agent side — web_fetch returns only the title or truncated nav, curl times out or gets 403, content is JS-rendered, Cloudflare/WAF shows "Just a moment" or "Under Attack", or a site works in the user's browser but fails from every automated client. Also for verifying official sources that block datacenter IPs.
---

# Accessing Blocked Sites

## Overview

An "unreachable" site is usually one of three problems, and each needs a different fix:

1. **Wrong egress.** Browsers follow the OS proxy (WinINET, `scutil`, `*_proxy`); `curl` and `urllib` do not. Some domains are reachable only through the proxy; others reject the proxy's exit IP and answer only on a direct connection.
2. **Cloudflare / WAF.** These separate a real browser from an HTTP client by TLS fingerprint, UA, JS challenge execution, and IP reputation.
3. **JS-rendered content.** A plain GET returns HTTP 200 and a shell of a page.

**Escalate in a fixed order and pick the next rung from the failure. Never report "cannot access" before the ladder is exhausted and the user has been offered a handoff.**

---

## Symptom → rung

| Symptom | Cause | Go to |
|---|---|---|
| `TypeError: fetch failed`, TCP timeout, connection reset | client ignores the system proxy | **Rung 2** |
| HTTP 200 but text is only the title, or only nav | content is JS-rendered | **Rung 4** |
| HTTP 403, `Server: cloudflare`, `Cf-Mitigated: challenge` | Cloudflare challenge | **Rung 4** |
| HTTP 403 from a custom WAF page ("prevented your request") | WAF, may still yield to a real browser | **Rung 4**, then **Rung 6** |
| 403 with `captcha-delivery.com`, `datadome`, `px-captcha` in the body | DataDome / PerimeterX on a flagged exit IP | **Rung 6** — a fresher browser will not clear it |
| HTTP 401 `bad IP reputation` (r.jina.ai) | proxy exit IP flagged | **Rung 4** or **Rung 6** |
| Chromium aborts with `ProcessSingleton ... Aborting` / `拒绝访问` | sandbox forbids the browser's ACL writes | policy must be `danger-full-access` |
| 200, but the file is a binary (PDF/DOCX) | not an HTML problem | download and parse locally |
| Works in the user's browser, fails direct **and** proxied | exit IP reputation | **Rung 6** — a different egress or a human, not another client |

**Control test first**, when every site fails:

```bash
python <skill-dir>/scripts/check_egress.py     # both paths, auto-detected proxy
curl --proxy <proxy> https://example.com        # single path
```

If the control fails, the problem is the environment, not the site.

---

## Rung 1 — `web_fetch` tool

Cheapest, fine for static pages. A very short body, or a body that begins with a long nav block, is a failure: that is the JS-render signature. Do not pass a title-only response down the pipeline as "the page".

---

## Rung 2 — the right egress

Two failures look identical from the agent side and have opposite fixes:

- the domain is unreachable from the local network and needs the proxy;
- the domain blocks the proxy's exit IP and needs the proxy **off**.

A site can therefore work in the user's browser and fail for the agent while both are online — the egress is part of the request. Test both directions, and say which one produced the bytes.

Nothing here assumes a port or a vendor. Detection order:

| Platform | Source |
|---|---|
| all | `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` |
| Windows | WinINET `ProxyServer`, else `AutoConfigURL` (PAC) |
| macOS | `scutil --proxy`, else its PAC |
| Linux | `*_proxy`, which every Linux proxy client sets |

Anything else is passed explicitly: `--proxy 10.0.0.5:3128`, `--proxy socks5://127.0.0.1:1080`, `--proxy none`. SOCKS is handed to the browser unchanged.

**Test the egress before blaming a target.** A proxy port can accept TCP and still be dead upstream: `CONNECT` answers 200, then the TLS handshake is dropped, or plain HTTP returns 502. That looks exactly like a blocked site and no browser escalation fixes it. `scripts/check_egress.py` probes both paths and prints a verdict: both work / proxy cannot carry traffic / proxy off / no internet.

### curl

```powershell
# Windows — use curl.exe, not curl (which may resolve to Invoke-WebRequest)
$k = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$proxy = ($k.ProxyServer -split ';')[0] -replace '^\w+=', ''
curl.exe -s -o out.html -w '%{http_code}' --proxy "http://$proxy" -L --max-time 30 "<url>"
```

```bash
# macOS
proxy=$(scutil --proxy | awk '/HTTPProxy/{h=$3} /HTTPPort/{p=$3; print h":"p; exit}')
curl -s -o out.html -w '%{http_code}' --proxy "http://$proxy" -L --max-time 30 "<url>"

# Linux
curl -s -o out.html -w '%{http_code}' --proxy "$HTTPS_PROXY" -L --max-time 30 "<url>"
```

`ProxyServer` may be a bare `host:port` or a `http=a:1;https=b:2` list; take the first entry.

### When the proxy is a PAC file

Split-tunnel tools publish a PAC instead of enabling the static proxy, so `scutil --proxy` shows `HTTPEnable : 0` beside `ProxyAutoConfigEnable : 1`. That is **not** "no proxy" — the PAC names the endpoint. Resolve it, then test the candidate before trusting it:

```bash
scutil --proxy | grep -E 'ProxyAutoConfig|Enable|Proxy|Port'
curl -s -o /dev/null -w '%{http_code}\n' --proxy http://<host:port> https://example.com
```

A PAC can name several endpoints, including a split-tunnel helper that accepts TCP but proxies only one domain. When a PAC and a static proxy are both configured, the browser follows the PAC, so the egress the script forces and the one the user's browser uses can differ — report which one produced the text. `--proxy auto` parses the PAC, resolves it per host, and probes each candidate.

---

## Rung 3 — browser-like headers

Weak sites filter on UA only:

```bash
curl -s -L --max-time 30 \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" \
  -H "Accept-Language: en-US,en;q=0.9" \
  -H "Sec-Fetch-Mode: navigate" -H "Sec-Fetch-Site: none" \
  --proxy "http://$proxy" "<url>"
```

This does nothing against Cloudflare; do not linger here.

---

## Rung 4 — real browser over CDP (the workhorse)

`scripts/fetch_page.py` launches a **dedicated** browser profile, proxies it, forces a real UA, opens the page, and polls until the challenge clears.

```bash
# <skill-dir> = this skill's folder
python <skill-dir>/scripts/fetch_page.py "<url>" "<out_prefix>" --headed
```

Skill locations: DSH `~/.dsh/skills/accessing-blocked-sites/`, Claude Code `~/.claude/skills/...`, Codex/Gemini `~/.agents/skills/...`. If the script is elsewhere, pass its path.

### Flags that cannot be skipped

| Flag | Why |
|---|---|
| **`--headed`** | Headless Chromium fails a Cloudflare challenge even with a spoofed UA. A real window usually clears it; clearance can take up to 90 s. Cost: a window appears on the user's screen. |
| **UA override** | Playwright/headless advertises `HeadlessChrome`. The script rebuilds the UA from the real browser version in a fresh context. |
| **challenge polling** | Clearance takes seconds to a minute. Read `challenge_cleared` and `text_chars`, never the exit code alone. |
| **`--proxy auto`** | OS proxy for this host, with per-domain PAC resolution. `--proxy none` forces DIRECT. |
| **`--browser auto`** | Picks Edge/Chrome/Chromium; override with `--browser` or `--browser-path`. |
| **fresh by default** | An already-running CDP endpoint ignores `--headed`, so a headed retry can look exactly like the headless failure. The default stops a leftover browser first. |
| **`--insecure`** | Some sites have broken certificate chains; use only when needed. |
| **`--no-sandbox`** | Containers/root only where Chromium refuses to start; it reduces isolation. |
| **`--wait 90`** | Cloudflare sometimes needs longer than 60 s; raise it instead of retrying blindly. |

Success is `"challenge_cleared": true` with `text_chars` in the thousands. Also read `blocker`: `datadome`, `perimeterx` or `akamai` means the exit IP is the problem, and another attempt on the same IP adds nothing — go to Rung 6. The script writes `<prefix>.txt` (rendered text) and `<prefix>.html`.

Exit codes: `0` cleared and substantial, `1` loaded but challenged or too short, `2` setup problem (no browser, no Playwright). Judge by the JSON: a Playwright warning on stderr can also produce a non-zero status.

### Playwright

```bash
pip install playwright
```

`playwright install chromium` is **not** needed — the script drives the installed browser over CDP. If Playwright is missing it reports `stage: "playwright"`; fall back to Rungs 2/5/6.

Where the skill ships a bundled interpreter, `<skill-dir>/.venv/bin/python` already has it:

```bash
<skill-dir>/.venv/bin/python <skill-dir>/scripts/fetch_page.py "<url>" "<out_prefix>" --headed
```

`batch_fetch.py` resolves an interpreter in this order: `.venv/bin/python`, a system Python with Playwright, `uv run --with playwright`.

### When the browser cannot start

- `No Chromium-family browser found` → install Edge/Chrome/Chromium, or pass `--browser-path`.
- `ProcessSingleton ... Aborting` / `拒绝访问` → the sandbox blocks the profile writes. In DSH switch to `danger-full-access`; do not retry under `workspace-write`.
- Port conflict → `--port 9336`.
- No window with `--headed` → a leftover headless instance is being reused; stop it (the script does by default).

---

## Rung 4b — batch mode

`scripts/batch_fetch.py` runs Rung 4 over a list, one browser per URL with its own profile and CDP port, and grades every result:

```bash
python <skill-dir>/scripts/batch_fetch.py urls.txt -j 3 --wait 60 --out out/run1
```

Outputs: `index.md` (pass/fail table to report from), `results.jsonl`, `corpus.md` (successful pages concatenated for a model), `pages/<n>-<slug>.txt|.html`.

The egress is chosen **per URL**, not globally. `--route auto` (default) probes the direct and proxied path for each URL, runs the one that answered, and falls back to the other once; `index.md` records which path each URL needed. `--route direct` and `--route proxy` force one path. A PAC is resolved per host, so a domain it sends direct is not probed through its proxy endpoint, and `--proxy none` really means DIRECT.

Statuses: `ok`, `short`, `blocked-cloudflare`, `blocked-datadome`, `blocked-perimeterx`, `blocked-akamai`, `timeout`, `error`.

Three rules:

- `blocked-datadome` / `blocked-perimeterx` / `blocked-akamai` mean **stop and hand off** (Rung 6); rerunning on the same IP changes nothing.
- If every URL fails with `stage: "playwright"`, no interpreter with Playwright was found.
- Report coverage, not just successes: "8/10 read" is a finding; silently delivering 8 files is not.

---

## Rung 5 — side doors (no browser needed)

| Door | Try | Caveat |
|---|---|---|
| static CDN host | `staticcdn.<domain>` file paths | only files whose paths you already know |
| staging / mirror host | `*.azurefd.net`, `*-staging*`, dev hosts | often older URL structure |
| CMS REST API | `/<cms-root>/wp-json/wp/v2/pages?search=<kw>` | `content` is often empty on builder-rendered sites |
| text proxy | `https://r.jina.ai/<url>` | 401 when the exit IP has bad reputation |
| Wayback | `https://archive.org/wayback/available?url=` then `/web/<ts>/<url>` | many pages have no snapshot; the CDX API is sometimes offline |

---

## Rung 6 — hand off to the user

When a WAF rejects every automated client, the user's browser still works. Offer all three, cheapest first:

1. **Copy the text:** click in the page body, `Ctrl+A`, `Ctrl+C`, paste into chat.
2. **Save the page:** `Ctrl+S`, "Webpage, HTML only", into a folder the agent can read.
3. **Screenshot:** the agent reads images directly, often faster than text.

Do **not** ask for session cookies unless the user offers them. A `cf_clearance` they volunteer is used only for that host and never written to disk.

A DevTools console has no prompt line while the Console drawer is open (toggled with `Esc` in Edge); if the user reports "下面没有", have them press `Esc`, or switch to copy/save/screenshot.

**Stop rule:** after three automated rungs fail, hand off. One `Ctrl+A` from the user beats ten more automated attempts.

---

## Hard rules

- **Never** point `--user-data-dir` / `--profile` at the user's real browser profile (`%LOCALAPPDATA%\Microsoft\Edge\User Data`, `~/Library/Application Support/Google/Chrome`, `~/.config/google-chrome`). Browsers cannot unwrap App-Bound (`v20`) cookie keys outside their own profile and **silently delete every cookie they cannot decrypt**.
- **Never** kill all browser processes. Kill only processes whose command line contains this skill's dedicated profile path (`dsh-fetch-profile`); the script does this.
- **Stop the browser when done** (the script does unless `--keep`).
- The browser needs **`danger-full-access`** under DSH. Under `workspace-write`, Chromium aborts with `ProcessSingleton ... Aborting` and `sandbox access to cache directory ... 拒绝访问`; do not retry under the sandbox.
- **Report the rung** that produced the text, so the user can judge evidence strength.
- Extract only what the task needs; these pages are large.
- Respect the site: no credential stuffing, no bypassing logins or paywalls, no scraping personal data beyond the page the task needs.
- Do not record visited sites or site-specific findings in this skill. Evidence belongs in the task output.
- "The proxy is on" does not mean "the proxy is correct": the right egress is a property of the target domain.

---

## Common mistakes

- Declaring a site blocked after one failed client — the proxy rung is missed most often.
- Trusting `--headed` while a browser is already running: the flag is ignored and the retry looks identical to the failure.
- Reading the exit code instead of `challenge_cleared` / `text_chars`.
- Treating a title-only `web_fetch` response as the page.
- Spending more rounds on the target than on the handoff.
- Pointing the browser at the real profile and destroying the user's cookies.
- Forgetting that macOS/Linux `curl` also needs the proxy; the browser uses it automatically.
- Reading `HTTPEnable : 0` as "no proxy" while a PAC is configured.
- Rerunning a DataDome/PerimeterX list instead of handing off: the exit IP is the blocker.

---

## Checklist

- [ ] Did `check_egress.py` show at least one path that carries traffic?
- [ ] Did I try the target **with** and **without** the proxy?
- [ ] If `HTTPEnable : 0`, did I read the PAC instead of concluding "no proxy"?
- [ ] Is the body substantial, or just a shell/nav/title?
- [ ] If Cloudflare: did I use `--headed`, `--wait 90`, and check `challenge_cleared`?
- [ ] Did I record which rung produced the text?
- [ ] For a list, did I use `batch_fetch.py` and report `index.md` coverage?
- [ ] If all automated rungs failed, did I offer copy/save/screenshot?
