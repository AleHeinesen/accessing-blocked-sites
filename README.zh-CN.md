# accessing-blocked-sites

> **English: [README.md](README.md)** · 以下是中文

一个让 Agent「读得到网页」的技能：换对出口、起真浏览器、过挑战，读不到就如实说。

Cloudflare / WAF 靠四样东西区分真浏览器和 HTTP 客户端：TLS 指纹、UA、JS 挑战、IP 信誉。**出口也是其中一条**——有的域名只有走代理才通，有的域名拒绝代理出口、只在直连下应答。这个技能两条轴都处理，并按固定顺序升级。

自包含：一个 `SKILL.md` 加三个脚本。无框架、无服务、无账号。

## 做什么

1. **按 URL 选出口**：读系统代理（`*_proxy` / WinINET / `scutil`+PAC），再探测直连与代理两条路，走通哪条用哪条
2. **固定顺序升级**：`web_fetch` → 带正确出口的 curl → 浏览器头 → CDP 真浏览器 → 侧门 → 交回给人
3. **轮询过挑战**：UA 用真实浏览器版本重建，独立 profile，跑完即关
4. **每个结果都有判定**：`ok` / `short` / `blocked-cloudflare` / `blocked-challenge` / `blocked-datadome` / `blocked-perimeterx` / `blocked-akamai` / `timeout` / `error`
5. **报覆盖率**，不只报成功；没读到的就一直是没读到

```
check_egress.py    网络本身通不通，哪条路通
fetch_page.py      一个 URL → 正文 + HTML + JSON 判定
batch_fetch.py     一批 URL → index.md、results.jsonl、corpus.md、pages/
```

## 安装

需要 Python 3.10+ 和本机已装的 Chrome / Edge / Chromium。有 `uv` 就用 `uv`，否则 `python -m venv`。

```bash
git clone https://github.com/AleHeinesen/accessing-blocked-sites && cd accessing-blocked-sites
python install.py
```

```bash
python install.py --target claude        # dsh | claude | cursor | agents
python install.py --dir /path/to/skills
python install.py --dry-run
python install.py --no-venv              # 用自己的解释器
```

安装器会复制技能、在脚本旁建 `.venv` 并装 Playwright。不会碰你真实的浏览器配置。

手动安装：把 `skills/accessing-blocked-sites` 复制进技能目录，再 `python -m pip install playwright`。**不需要** `playwright install`——脚本用 CDP 驱动你已有的浏览器。

装完重启 Agent。

## 用法

单页：

```bash
python skills/accessing-blocked-sites/scripts/fetch_page.py <url> out/page --headed
```

批量——每个 URL 一个浏览器、独立 profile 与 CDP 端口、按 URL 选出口：

```bash
python skills/accessing-blocked-sites/scripts/batch_fetch.py urls.txt -j 3 --wait 60 --out out/run1
```

先检查网络，再怪站点：

```bash
python skills/accessing-blocked-sites/scripts/check_egress.py
```

## 设计要点

- **`--headed` 不能省**：无头 Chromium 即使伪装 UA 也过不了多数 Cloudflare 挑战；真窗口通常一分钟内能过，代价是屏幕上出现一个窗口。
- **`--proxy none` 真的直连**：否则 Chromium 仍会读系统 PAC，「直连重试」会悄悄复用同一出口。
- **PAC 按域名解析**：分流域名的 PAC 只对这些域名用代理，把它的端点套到所有站点是 bug。
- **只用独立 profile**：浏览器无法在自身 profile 之外解密 App-Bound（v20）cookie，会静默删除解不开的条目。指向真实 profile 等于毁掉 cookie。
- **两条路都失败，问题在出口 IP**：换出口或找人，不是换客户端。
- **文本长度按语言折算**：`--min-text` 以「拉丁字符等量」比较，一个中文字符按两个算，所以中文好页不会在默认 400 下被判成 `short`。JSON 判定里 `text_weight` 与 `text_chars` 并列给出。
- **封禁方只认结构证据**：`blocker` 从标题与 `src` / `href` / `action` / `data-*` 属性里匹配，正文只在「短到可能是拦截页」（不足 1500 字符）时才扫；讨论 Cloudflare 或 DataDome 的页面不会被判成被它拦截。

## 它不做什么

- 不解验证码、不代登录、不处理 2FA——交回给你
- 不绕付费墙、不抓个人数据；遵守站点条款与 `robots.txt`
- 不是爬虫框架，只读你给的 URL

## 状态

能处理静态页、JS 渲染页、Cloudflare 挑战和 DataDome 保护页。它解决不了的——出口 IP 已被目标站点记账——会作为缺口报出来，而不是反复重试。

## 配套

[`research-report`](https://github.com/AleHeinesen/research-report) 把它当抓取层，额外提供子查询拆解、覆盖核对、引用校验与带出处的报告。装那个技能会一并装上本技能。

## 许可

MIT，见 [LICENSE](LICENSE)。
