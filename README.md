# accessing-blocked-sites

English | [简体中文](README.zh-CN.md)

## 中文简介

让 Agent 读得到那些读不到的网页：按站点在「直连 / 代理」之间选出口，用本机真实 Chromium 过 Cloudflare，读不到时给出原因和覆盖率，而不是猜。

- **安装**：`git clone` 后 `python install.py`，自动建 venv 装 Playwright；**不需要** `playwright install`
- **单页**：`scripts/fetch_page.py <url> out/page --headed`
- **批量**：`scripts/batch_fetch.py urls.txt -j 3 --wait 60 --out out/run1`
- **产物**：`index.md`（每页实际走的出口）、`results.jsonl`、`corpus.md`、`pages/`
- **边界**：不解验证码、不代登录、不绕付费墙；出口 IP 被记账时报成缺口，不反复重试

完整中文文档见 [README.zh-CN.md](README.zh-CN.md)。

---


An agent skill that reads pages other clients cannot.

Cloudflare, WAFs and bot managers separate a real browser from an HTTP client by TLS fingerprint, UA, JS challenge execution and IP reputation. They also separate one *egress* from another: some domains answer only through a proxy, others reject the proxy's exit IP and answer only on a direct connection. This skill handles both axes, in a fixed order, and reports honestly when it fails.

It is self-contained: one `SKILL.md` and three scripts. No framework, no service, no account.

## What it does

1. **Tests the right egress per URL.** Reads the OS proxy (`*_proxy`, WinINET, `scutil` + PAC), then probes direct and proxied paths and runs the one that answers.
2. **Escalates in order**: `web_fetch` → curl with the right egress → browser-like headers → real Chromium over CDP → side doors → hand off to you.
3. **Clears challenges** by polling, with a UA rebuilt from the real browser version and a fresh dedicated profile.
4. **Grades every result** instead of guessing: `ok`, `short`, `blocked-cloudflare`, `blocked-datadome`, `blocked-perimeterx`, `blocked-akamai`, `timeout`, `error`.
5. **Reports coverage**, not just successes. Unread pages stay unread.

```
check_egress.py    is the network healthy, on which path?
fetch_page.py      one URL -> text + html + JSON verdict
batch_fetch.py     many URLs -> index.md, results.jsonl, corpus.md, pages/
```

## Install

Requires Python 3.10+ and an installed Chrome, Edge or Chromium. `uv` is used when present, otherwise `python -m venv`.

```bash
git clone https://github.com/AleHeinesen/accessing-blocked-sites && cd accessing-blocked-sites
python install.py                # detects ~/.dsh, ~/.claude, ~/.cursor or ~/.agents
```

```bash
python install.py --target claude        # or dsh | cursor | agents
python install.py --dir /path/to/skills  # explicit destination
python install.py --dry-run
python install.py --no-venv              # bring your own interpreter
```

The installer copies the skill, provisions a `.venv` next to the scripts and installs Playwright into it. It never touches your real browser profile.

Manual install: copy `skills/accessing-blocked-sites` into your skills directory, then `python -m pip install playwright`. `playwright install` is **not** needed — the scripts drive the browser already on the machine.

Restart the agent afterwards so it picks up the new skill.

## Use

One page:

```bash
python skills/accessing-blocked-sites/scripts/fetch_page.py <url> out/page --headed
```

A list — one browser per URL, its own profile and CDP port, per-URL egress choice:

```bash
python skills/accessing-blocked-sites/scripts/batch_fetch.py urls.txt -j 3 --wait 60 --out out/run1
```

Outputs:

| File | Content |
|---|---|
| `index.md` | pass/fail table, including which egress each URL used |
| `results.jsonl` | one record per URL |
| `corpus.md` | successful pages concatenated, ready to feed a model |
| `pages/<n>-<slug>.txt/.html` | per-URL text and raw HTML |

Check the network before blaming a site:

```bash
python skills/accessing-blocked-sites/scripts/check_egress.py
```

It probes both paths and prints a verdict: both work / proxy cannot carry traffic / proxy off / no internet.

## Design notes

- **The `--headed` rule.** Headless Chromium fails many Cloudflare challenges even with a spoofed UA. A real window usually clears it, within a minute or so. The cost is a window on your screen.
- **`--proxy none` means DIRECT.** Chromium would otherwise still follow the system PAC, so a "direct" retry would silently reuse the same egress.
- **PAC is resolved per host.** A split-tunnel PAC proxies specific domains and sends the rest direct; using its endpoint for everything is a real bug, not a preference.
- **Dedicated profile, always.** Browsers cannot decrypt App-Bound (`v20`) cookies outside their own profile and will silently delete what they cannot unwrap. Pointing this at your real profile destroys cookies.
- **Headless and headed are one flag apart, but the result differs.** If a site fails both, the blocker is the exit IP, and the answer is a different egress or a human — not another client.

## What it is not

- It does not solve CAPTCHAs, logins or 2FA. Those are handed back to you.
- It does not bypass paywalls or scrape personal data. Respect the site's terms and `robots.txt`.
- It is not a spider: it reads the URLs you give it.

## Status

Handles static pages, JS-rendered pages, Cloudflare challenges and Datadome-protected pages. What it cannot fix — an exit IP that the target has already scored — is reported as a gap rather than retried.

## Pairs with

[`research-report`](https://github.com/AleHeinesen/research-report) uses this skill as its fetch layer and adds sub-query planning, coverage checks, quote verification and cited reports. Installing that skill also installs this one.

## License

MIT — see [LICENSE](LICENSE).
