"""Generate a static paper-radar site from data/daily/*.json records.

Reads daily JSON snapshots produced by the zotero-arxiv-daily pipeline and
emits a pure static HTML/JS site (no secrets, no backend) under an output
directory, ready to be served over HTTP (e.g. a tailnet site).

Usage:
    python tools/generate_site.py                       # defaults: data/daily -> /workspace/sites/papers
    python tools/generate_site.py --source data/daily --output /workspace/sites/papers
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "data" / "daily"
DEFAULT_OUTPUT = Path("/workspace/sites/papers")


def _iso(d: date | str) -> str:
    return d.isoformat() if isinstance(d, date) else str(d)


def load_records(source_dir: Path) -> list[dict]:
    records = []
    for path in sorted(source_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(record, dict) or "date" not in record:
            continue
        records.append(record)
    records.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    return records


def _author_list(authors) -> str:
    if not authors:
        return "未知作者"
    authors = [str(a) for a in authors]
    if len(authors) <= 3:
        return "、".join(authors)
    return "、".join(authors[:3]) + f" 等 {len(authors)} 人"


def build_page_data(records: list[dict]) -> list[dict]:
    """Convert raw daily records into a compact, secret-free page dataset."""
    pages = []
    for record in records:
        recs = []
        for item in record.get("recommendations", []):
            assessment = item.get("assessment") or {}
            contribution = assessment.get("contribution") or {}
            why_care = assessment.get("why_care") or {}
            recs.append(
                {
                    "rank": item.get("rank"),
                    "title": item.get("title") or "",
                    "authors": _author_list(item.get("authors")),
                    "categories": item.get("categories") or [],
                    "url": item.get("url") or "",
                    "pdf_url": item.get("pdf_url") or "",
                    "source": item.get("source") or "",
                    "paper_id": item.get("paper_id") or "",
                    "published_at": item.get("published_at"),
                    "relevance": assessment.get("relevance"),
                    "bucket": assessment.get("bucket") or "",
                    "tldr_zh": contribution.get("zh") or "",
                    "why_care_zh": why_care.get("zh") or "",
                    "deep_read": item.get("deep_read") or {},
                    "channel_scores": item.get("channel_scores") or {},
                }
            )
        pages.append(
            {
                "date": record.get("date"),
                "status": record.get("status"),
                "retrieved_count": record.get("retrieved_count"),
                "candidate_count": record.get("candidate_count"),
                "recommendation_count": record.get("recommendation_count"),
                "delivery_status": record.get("delivery_status"),
                "recommendations": recs,
            }
        )
    return pages


# --- 页头 logo / favicon：同一枚小图标（翻开的小册子 + 一条藕荷色书签） ---------
# 两处共用同一组路径，只是颜色写法不同（页头走 CSS 变量，favicon 写死颜色）。
LOGO_SHAPES = (
    '<path d="M12 7C9.8 5.6 6.6 5.2 3.5 5.8v12.4c3.1-.6 6.3-.2 8.5 1.2 2.2-1.4 5.4-1.8 8.5-1.2V5.8C17.4 5.2 14.2 5.6 12 7Z" '
    'fill="{wash}" stroke="{line}" stroke-width="1.5" stroke-linejoin="round"/>'
    '<path d="M12 7v12.4" fill="none" stroke="{line}" stroke-width="1.3" stroke-linecap="round"/>'
    '<path d="M6.2 9.6c1.3-.1 2.6.1 3.7.6M6.2 12.4c1.3-.1 2.6.1 3.7.6" fill="none" stroke="{line}" '
    'stroke-width="1.1" stroke-linecap="round" opacity=".55"/>'
    '<path d="M15.6 5.5v6.2l1.45-1.15 1.45 1.15V5.2" fill="{line}" stroke="{line}" stroke-width="1" stroke-linejoin="round"/>'
)
LOGO_LINE = "#8f7bb3"
LOGO_WASH = "#f1ebf8"


def logo_svg_inline() -> str:
    shapes = LOGO_SHAPES.format(wash="var(--logo-wash)", line="var(--lilac)")
    return (
        '<svg class="logo" viewBox="0 0 24 24" width="26" height="26" aria-hidden="true" focusable="false">'
        + shapes
        + "</svg>"
    )


def favicon_href(version: int = 3) -> str:
    # 版本参数写成 SVG 末尾的 XML 注释：data: URI 后直接拼 "?v=3" 会让 Chromium 解析失败，
    # 注释在根元素之后是合法的，href 里仍带有 "?v=3"。
    shapes = LOGO_SHAPES.format(wash=LOGO_WASH, line=LOGO_LINE)
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">' + shapes + "</svg>"
    )
    return "data:image/svg+xml," + quote(svg, safe="/=:,;()'") + quote(f"<!--?v={version}-->", safe="/=:,;()'!-?")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="#efebe4">
<title>arXiv 日报 · 紫璃</title>
<link rel="icon" type="image/svg+xml" href="__FAVICON__">
<script>
try{var t=localStorage.getItem("zp:theme");
if(t?t==="dark":window.matchMedia&&matchMedia("(prefers-color-scheme: dark)").matches)document.documentElement.setAttribute("data-theme","dark");}catch(e){}
</script>
<style>
:root{
  --desk:#ece8e1; --paper:#faf8f4; --ink:#2b2833; --soft:#5f5a6b; --faint:#837d90;
  --rule:#e6e1d8; --lilac:#8f7bb3; --lilac-ink:#6e5a96; --lilac-wash:#f1ebf8; --logo-wash:#f1ebf8;
  --warn:#a0636f; --band:#f5f2f8; --edge:rgba(60,40,90,.045); --lift:0 1px 2px rgba(60,40,90,.06),0 18px 50px -18px rgba(60,40,90,.22);
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Source Han Serif SC","Noto Serif CJK SC","Noto Serif SC","Songti SC","STSong","SimSun",Georgia,"Times New Roman",serif;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Noto Sans CJK SC","Noto Sans SC","Microsoft YaHei",Roboto,sans-serif;
}
[data-theme="dark"]{
  --desk:#0d0b12; --paper:#16131d; --ink:#e4dfea; --soft:#b2abbf; --faint:#847d94;
  --rule:#2a2535; --lilac:#a794cc; --lilac-ink:#bfaee2; --lilac-wash:#231e31; --logo-wash:#231e31;
  --warn:#c98e9a; --band:#1d1927; --edge:rgba(0,0,0,.35); --lift:0 1px 2px rgba(0,0,0,.5),0 18px 50px -18px rgba(0,0,0,.7);
}
*{ box-sizing:border-box; }
html{ -webkit-text-size-adjust:100%; background:var(--desk); }
body{ margin:0; background:var(--desk); color:var(--ink); font-family:var(--sans); line-height:1.6;
  -webkit-font-smoothing:antialiased; transition:background .3s ease,color .3s ease; }
a{ color:inherit; text-decoration:none; }
button,select{ font:inherit; color:inherit; }
:focus-visible{ outline:2px solid var(--lilac); outline-offset:3px; border-radius:3px; }

.content{ flex:1; display:flex; flex-direction:column; min-width:0; }
.side-no{ display:none; }
.e-main{ container-type:inline-size; }
.sheet{ max-width:40rem; margin:0 auto; min-height:100vh; min-height:100dvh; background:var(--paper);
  padding:0 1.5rem calc(2.5rem + env(safe-area-inset-bottom)); display:flex; flex-direction:column;
  transition:background .3s ease; }

/* 页头 */
.masthead{ display:flex; align-items:center; justify-content:space-between; padding:1.15rem 0 .9rem; }
.brand{ display:flex; align-items:center; gap:.6rem; font-family:var(--serif); font-size:1rem; letter-spacing:.14em; color:var(--soft); }
.logo{ display:block; flex:none; }
.brand small{ font-family:var(--sans); font-size:.72rem; letter-spacing:.04em; color:var(--faint); margin-left:.15rem; }
.icon-btn{ display:inline-grid; place-items:center; width:2.5rem; height:2.5rem; margin-right:-.5rem; border:0; background:none;
  color:var(--faint); border-radius:50%; cursor:pointer; transition:color .2s,background .2s; }
.icon-btn:hover{ color:var(--lilac-ink); background:var(--lilac-wash); }
.icon-btn svg{ width:19px; height:19px; display:block; }
.icon-btn .i-sun{ display:none; }
[data-theme="dark"] .icon-btn .i-sun{ display:block; }
[data-theme="dark"] .icon-btn .i-moon{ display:none; }

/* 每天一页：页码式日期 */
.folio{ padding:2.4rem 0 1.4rem; }
.folio-year{ margin:0 0 .35rem; font-size:.78rem; letter-spacing:.16em; color:var(--faint); }
.folio-day{ margin:0; display:flex; align-items:baseline; gap:.7rem; font-family:var(--serif); font-weight:400; line-height:1; }
.folio-day .num{ font-size:4.6rem; letter-spacing:-.02em; color:var(--ink); font-variant-numeric:lining-nums; }
.folio-day .mon{ font-size:1.05rem; letter-spacing:.2em; color:var(--faint); }
.stats{ margin:1rem 0 0; font-size:.78rem; color:var(--faint); letter-spacing:.03em; }
.stats .dot{ margin:0 .45em; opacity:.6; }
.stats .warn{ color:var(--warn); }

/* 翻页条 */
.pager{ display:flex; align-items:center; gap:.25rem; margin:1.1rem 0 0; padding:.35rem 0; border-top:1px solid var(--rule); border-bottom:1px solid var(--rule); }
.pager .icon-btn{ margin:0; width:2.4rem; height:2.4rem; flex:none; }
.pager .icon-btn[disabled]{ opacity:.28; cursor:default; pointer-events:none; }
.pick{ flex:1; min-width:0; position:relative; }
.pick select{ width:100%; appearance:none; -webkit-appearance:none; border:0; background:transparent; text-align:center; text-align-last:center;
  font-family:var(--serif); font-size:.95rem; letter-spacing:.06em; color:var(--soft); padding:.55rem .5rem; cursor:pointer; border-radius:6px; }
.pick select:hover{ color:var(--lilac-ink); }
.pick select option{ background:var(--paper); color:var(--ink); }
.latest{ flex:none; border:0; background:none; cursor:pointer; font-size:.78rem; letter-spacing:.08em; color:var(--lilac-ink);
  padding:.5rem .6rem; border-radius:999px; margin-right:-.4rem; }
.latest:hover{ background:var(--lilac-wash); }
.latest[disabled]{ visibility:hidden; }

/* 书页 */
.page{ flex:1; padding-top:2.4rem; }
.page.turn-next{ animation:turnNext .38s cubic-bezier(.2,.7,.2,1); }
.page.turn-prev{ animation:turnPrev .38s cubic-bezier(.2,.7,.2,1); }
@keyframes turnNext{ from{ opacity:0; transform:translateX(14px); } to{ opacity:1; transform:none; } }
@keyframes turnPrev{ from{ opacity:0; transform:translateX(-14px); } to{ opacity:1; transform:none; } }

.entry + .entry{ margin-top:3.1rem; padding-top:3.1rem; position:relative; }
.entry + .entry::before{ content:""; position:absolute; top:0; left:0; width:2.4rem; border-top:1px solid var(--rule); }
.entry .links{ margin-top:0; padding-top:1.5rem; }
.entry-head{ display:flex; align-items:baseline; gap:.7rem; margin-bottom:.75rem; }
.no{ font-family:var(--serif); font-style:italic; font-size:.95rem; color:var(--lilac); letter-spacing:.04em; }
.bucket{ font-size:.72rem; letter-spacing:.14em; color:var(--faint); }
.entry h2{ margin:0; font-family:var(--serif); font-weight:600; font-size:clamp(1.5rem,6.2vw,1.95rem); line-height:1.28;
  letter-spacing:-.005em; text-wrap:balance; overflow-wrap:break-word; hyphens:auto; color:var(--ink); }
.entry h2 a{ background:linear-gradient(var(--lilac),var(--lilac)) 0 100%/0 1px no-repeat; transition:background-size .3s ease,color .2s; }
.entry h2 a:hover{ color:var(--lilac-ink); background-size:100% 1px; }
.byline{ margin:.95rem 0 0; font-size:.82rem; color:var(--faint); line-height:1.7; }
.byline .sep{ margin:0 .4em; opacity:.6; }
.cats{ font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace; font-size:.76rem; letter-spacing:.02em; }
.rel{ display:inline-flex; align-items:center; gap:.6rem; margin-top:.9rem; font-size:.76rem; color:var(--faint); letter-spacing:.04em; }
.dots{ display:inline-flex; gap:3px; }
.dots i{ width:5px; height:5px; border-radius:50%; background:var(--rule); display:block; }
[data-theme="dark"] .dots i{ background:#2f2a3c; }
.dots i.on{ background:var(--lilac); }

.label{ display:block; margin:0 0 .35rem; font-size:.7rem; letter-spacing:.2em; color:var(--faint); }
.tldr{ margin:1.5rem 0 0; }
.tldr p{ margin:0; font-family:var(--serif); font-size:1.08rem; line-height:1.85; color:var(--ink); }
.why{ margin:1.35rem 0 0; padding-left:1rem; border-left:2px solid color-mix(in srgb,var(--lilac) 55%,transparent); }
.why p{ margin:0; font-size:.93rem; line-height:1.8; color:var(--soft); }
.links{ margin-top:1.5rem; display:flex; gap:.6rem; flex-wrap:wrap; }
.link{ display:inline-block; padding:.42rem 1.1rem; border-radius:999px; font-size:.84rem; letter-spacing:.06em;
  border:1px solid color-mix(in srgb,var(--lilac) 45%,transparent); color:var(--lilac-ink); transition:background .2s,border-color .2s; }
.link:hover{ background:var(--lilac-wash); border-color:var(--lilac); }
.link.main{ background:var(--lilac-wash); }
  .ds-tag{ display:inline-block; flex:0 0 auto; width:6.2rem; font-size:.78rem; letter-spacing:.16em; color:var(--lilac-ink); font-weight:600; }
  .ds-sent{ margin:0 0 .4rem 0; }
  .ds-line{ display:flex; gap:.75rem; margin:0 0 .45rem 0; line-height:1.62; }
  .ds-text{ flex:1; min-width:0; font-size:.94rem; line-height:1.62; color:var(--ink); overflow-wrap:break-word; }
  .lesson{ margin:1.1rem 0 0; border:1px solid var(--rule); border-radius:6px; background:var(--band); }
  .lesson summary{ padding:.55rem .8rem; font-size:.84rem; letter-spacing:.08em; color:var(--lilac-ink); cursor:pointer; outline:none; list-style:none; }
  .lesson summary::-webkit-details-marker{ display:none; }
  .lesson summary:hover{ color:var(--lilac); }
  .lesson[open] summary{ border-bottom:1px solid var(--rule); padding-bottom:.5rem; margin-bottom:.5rem; }
  .lesson-body{ padding:0 .8rem .7rem; }
  .lesson-item{ margin:.45rem 0; font-size:.9rem; line-height:1.7; color:var(--ink); }
  .lesson-concept{ display:block; font-weight:600; color:var(--lilac-ink); letter-spacing:.08em; margin-bottom:.12rem; }
  .lesson-link{ margin-top:.22rem; font-size:.78rem; color:var(--soft); }

/* 空白页 */
.blank{ text-align:center; padding:3.2rem 0 2rem; }
.blank .mark{ width:34px; height:34px; margin:0 auto 1.4rem; opacity:.9; display:block; }
.blank p{ margin:0; }
.blank .big{ font-family:var(--serif); font-size:1.2rem; letter-spacing:.14em; color:var(--soft); }
.blank .small{ margin-top:.7rem; font-size:.82rem; color:var(--faint); line-height:1.8; }

/* 页脚 */
.turn{ display:flex; align-items:center; justify-content:space-between; margin-top:3.4rem; padding-top:1rem; border-top:1px solid var(--rule); font-size:.82rem; color:var(--faint); }
.turn button{ border:0; background:none; cursor:pointer; color:var(--soft); letter-spacing:.06em; padding:.55rem 0; }
.turn button:hover{ color:var(--lilac-ink); }
.turn button[disabled]{ opacity:.3; cursor:default; pointer-events:none; }
.turn .no-of{ font-family:var(--serif); font-style:italic; letter-spacing:.1em; }
.colophon{ margin:1.6rem 0 0; font-size:.72rem; line-height:1.8; color:var(--faint); text-align:center; opacity:.85; }

.hidden{ display:none !important; }
@media (min-width:700px){
  body{ padding:2.4rem 1.5rem 3rem; }
  .sheet{ min-height:0; padding:0 3.4rem 2.6rem; border-radius:5px; box-shadow:var(--lift);
    background-image:linear-gradient(90deg,var(--edge),transparent 1.6rem); }
  .masthead{ padding-top:1.7rem; }
  .folio{ padding-top:3rem; }
  .folio-day .num{ font-size:5.4rem; }
  .tldr p{ font-size:1.12rem; }
}
/* 桌面：左侧窄栏（日期 / 统计 / 翻页，sticky）+ 右侧论文多栏 */
@media (min-width:900px){
  body{ padding:2rem 2rem 2.4rem; }
  .sheet{ max-width:84rem; min-height:calc(100vh - 4.4rem); min-height:calc(100dvh - 4.4rem); padding:0 3rem 2.4rem;
    display:grid; grid-template-columns:15.5rem minmax(0,1fr); column-gap:0; align-items:start; flex-direction:row; }
  .rail{ position:sticky; top:2rem; align-self:start; max-height:calc(100vh - 4rem); padding-right:2.6rem; }
  .masthead{ padding-top:1.7rem; padding-bottom:0; }
  .folio{ padding:3.2rem 0 0; }
  .folio-day .num{ font-size:5.6rem; }
  .stats{ line-height:1.9; }
  .pager{ flex-wrap:wrap; margin-top:1.6rem; padding:.3rem 0 .4rem; border-bottom:0; }
  .pick{ order:0; flex:1 1 100%; }
  .pick select{ font-size:.9rem; letter-spacing:.03em; padding:.5rem 0; }
  .pager #prevBtn{ order:1; }
  .side-no{ display:block; order:2; flex:1; text-align:center; font-family:var(--serif); font-style:italic; font-size:.8rem; letter-spacing:.1em; color:var(--faint); }
  .latest{ order:3; margin-right:0; }
  .pager #nextBtn{ order:4; }
  .content{ align-self:stretch; border-left:1px solid var(--rule); padding:0; margin-top:.9rem; }
  .page{ padding-top:.4rem; }
  /* 一行一篇：左 = 序号/标题/作者/相关度，右 = TLDR/为何推荐/链接；相邻行底色交替，铺到纸页右缘 */
  .entry{ display:grid; grid-template-columns:minmax(0,26rem) minmax(0,1fr); column-gap:3.2rem; align-items:start;
    padding:1.8rem 3rem 1.8rem 2.6rem; margin:0 -3rem 0 0; }
  /* 桌面两栏：左 = 标题/作者/相关度 + 摘要按钮 + 先补一课；右 = 四句 */
  .entry > .entry-head, .entry > h2, .entry > .byline, .entry > .rel, .entry > .links, .entry > .tldr, .entry > .why, .entry > details.lesson{ grid-column: 1; }
  .entry > .four-sent{ grid-column: 2; grid-row: 1 / -1; align-self: start; }
  .entry:nth-child(even){ background:var(--band); }
  .entry + .entry{ margin-top:0; padding-top:1.8rem; }
  .entry + .entry::before{ display:none; }
  .entry h2{ font-size:clamp(1.28rem,5.2cqi,1.7rem); line-height:1.27; text-wrap:pretty; }
  .tldr{ margin-top:0; }
  .tldr p{ font-size:1.02rem; line-height:1.8; }
  .why{ margin-top:1.1rem; }
  .why p{ font-size:.9rem; line-height:1.75; }
  .entry .links{ padding-top:1.2rem; }
  .byline{ margin-top:.8rem; }
  .entry-head{ margin-bottom:.6rem; }
  .blank{ padding:5rem 0 3rem; }
  .turn{ display:none; }
  .colophon{ margin-top:auto; padding:2.4rem 0 0 2.6rem; text-align:left; }
}
@media (min-width:900px) and (max-height:960px){
  body{ padding-top:1.1rem; padding-bottom:1.1rem; }
  .sheet{ min-height:calc(100vh - 2.2rem); min-height:calc(100dvh - 2.2rem); padding-bottom:1.2rem; }
  .rail{ top:1.1rem; }
  .masthead{ padding-top:1.2rem; }
  .folio{ padding-top:2.2rem; }
  .content{ margin-top:1.2rem; }
  .page{ padding-top:0; }
  .entry, .entry + .entry{ padding-top:1.5rem; padding-bottom:1.5rem; }
  .byline{ margin-top:.6rem; }
  .rel{ margin-top:.6rem; }
  .why{ margin-top:.85rem; }
  .entry .links{ padding-top:.9rem; }
  .colophon{ padding-top:1.2rem; }
}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{ animation:none !important; transition:none !important; }
}
</style>
</head>
<body>
<div class="sheet">
 <aside class="rail">
  <header class="masthead">
    <div class="brand">__LOGO__<span>arXiv 日报</span></div>
    <button class="icon-btn" id="themeToggle" type="button" title="切换深色 / 浅色" aria-label="切换深色或浅色">
      <svg class="i-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 14.2A8 8 0 0 1 9.8 4a8 8 0 1 0 10.2 10.2Z"/></svg>
      <svg class="i-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4"/></svg>
    </button>
  </header>

  <section class="folio" aria-live="polite">
    <p class="folio-year" id="folioYear"></p>
    <h1 class="folio-day"><span class="num" id="folioNum">–</span><span class="mon" id="folioMon"></span></h1>
    <p class="stats" id="meta">翻开中……</p>
  </section>

  <nav class="pager" aria-label="翻页">
    <button class="icon-btn" id="prevBtn" type="button" title="前一天" aria-label="前一天"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.5 6 8.5 12l6 6"/></svg></button>
    <div class="pick"><select id="dateSelect" aria-label="选择日期"></select></div>
    <button class="icon-btn" id="nextBtn" type="button" title="后一天" aria-label="后一天"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9.5 6 6 6-6 6"/></svg></button>
    <span class="no-of side-no" id="pageNo2"></span>
    <button class="latest" id="latestBtn" type="button">最新</button>
  </nav>
 </aside>

 <div class="content">
  <main class="page" id="page">
    <div id="list"></div>
    <div class="blank hidden" id="empty"></div>
  </main>

  <div class="turn">
    <button id="prevBtn2" type="button">‹ 前一天</button>
    <span class="no-of" id="pageNo"></span>
    <button id="nextBtn2" type="button">后一天 ›</button>
  </div>
  <p class="colophon">紫璃的 arXiv 日报 · 数据来自本地运行，仅含论文元数据与摘要，不含任何密钥或个人邮箱。</p>
 </div>
</div>
<script>
(() => {
  const $ = id => document.getElementById(id);
  const root = document.documentElement;
  const themeKey = "zp:theme";
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  const syncThemeColor = () => themeMeta && themeMeta.setAttribute("content", root.getAttribute("data-theme") === "dark" ? "#0d0b12" : "#ece8e1");
  syncThemeColor();
  $("themeToggle").addEventListener("click", () => {
    const dark = root.getAttribute("data-theme") === "dark";
    if (dark) root.removeAttribute("data-theme"); else root.setAttribute("data-theme", "dark");
    try { localStorage.setItem(themeKey, dark ? "light" : "dark"); } catch (e) {}
    syncThemeColor();
  });

  const dateSelect = $("dateSelect"), latestBtn = $("latestBtn"), metaEl = $("meta"),
        listEl = $("list"), emptyEl = $("empty"), pageEl = $("page");
  const navBtns = { prev: [$("prevBtn"), $("prevBtn2")], next: [$("nextBtn"), $("nextBtn2")] };

  const WEEK = ["日", "一", "二", "三", "四", "五", "六"];
  const LOGO_MARK = __LOGO_JS__;

  let pages = [];       // 新 → 旧
  let currentDate = null;

  function escHtml(t){
    return String(t == null ? "" : t)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
  }
  function safeUrl(u){ return /^https?:\/\//i.test(u || "") ? u : ""; }
  function parts(iso){
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || "");
    if (!m) return null;
    const y = +m[1], mo = +m[2], d = +m[3];
    return { y, mo, d, wk: WEEK[new Date(y, mo - 1, d).getDay()] };
  }
  function fmtShort(iso){ const p = parts(iso); return p ? `${p.mo} 月 ${p.d} 日` : (iso || ""); }

  function renderSelector(){
    dateSelect.innerHTML = "";
    for (const p of pages){
      const o = document.createElement("option");
      const n = (p.recommendations || []).length;
      const pt = parts(p.date);
      o.value = p.date;
      o.textContent = `${fmtShort(p.date)}${pt ? " · 周" + pt.wk : ""} · ${n ? n + " 篇" : "空白"}`;
      dateSelect.appendChild(o);
    }
  }

  function dots(v){
    if (v == null) return "";
    const n = Math.max(0, Math.min(10, Math.round(Number(v)) || 0));
    let s = "";
    for (let i = 0; i < 10; i++) s += `<i class="${i < n ? "on" : ""}"></i>`;
    return `<span class="rel" role="img" aria-label="相关度 ${n}/10"><span class="dots">${s}</span><span>相关度 ${n}/10</span></span>`;
  }

  function entryHtml(r, i){
    const no = String(r.rank || i + 1).padStart(2, "0");
    const url = safeUrl(r.url), pdf = safeUrl(r.pdf_url);
    const cats = (r.categories || []).map(escHtml).join(" · ");
    const pub = r.published_at ? String(r.published_at).slice(0, 10) : "";
    const title = escHtml(r.title);
    const dr = r.deep_read || {};
    const hasDeep = !!(dr.problem || dr.why_now || dr.approach || dr.result);
    const fl = (dr.first_lesson || []).filter(Boolean);

    // 没有深读数据的历史日期：优雅降级，照旧显示 TLDR / 为何推荐
    if (!hasDeep){
      return `
        <article class="entry">
          <div class="entry-head"><span class="no">№ ${no}</span></div>
          <h2>${url ? `<a href="${escHtml(url)}" target="_blank" rel="noopener">${title}</a>` : title}</h2>
          <p class="byline">${escHtml(r.authors)}${cats || pub ? `<br>` : ""}${cats ? `<span class="cats">${cats}</span>` : ""}${cats && pub ? `<span class="sep">·</span>` : ""}${pub ? `发表 ${escHtml(pub)}` : ""}</p>
          ${dots(r.relevance)}
          <div class="tldr"><span class="label">TLDR</span><p>${escHtml(r.tldr_zh || "（暂无摘要）")}</p></div>
          ${r.why_care_zh ? `<div class="why"><span class="label">为何推荐</span><p>${escHtml(r.why_care_zh)}</p></div>` : ""}
          <div class="links">${buildLinks(url, pdf)}</div>
        </article>`;
    }

    const fourSent = `
      <div class="four-sent">
        <span class="label">本文在说什么</span>
        <div class="ds-sent">${buildFourSentLines(dr)}</div>
      </div>`;
    const lesson = buildLesson(fl);
    return `
      <article class="entry">
        <div class="entry-head"><span class="no">№ ${no}</span></div>
        <h2>${url ? `<a href="${escHtml(url)}" target="_blank" rel="noopener">${title}</a>` : title}</h2>
        <p class="byline">${escHtml(r.authors)}${cats || pub ? `<br>` : ""}${cats ? `<span class="cats">${cats}</span>` : ""}${cats && pub ? `<span class="sep">·</span>` : ""}${pub ? `发表 ${escHtml(pub)}` : ""}</p>
        ${dots(r.relevance)}
        ${fourSent}
        <div class="links">${buildLinks(url, pdf)}</div>
        ${lesson}
      </article>`;
  }

  function buildFourSentLines(dr){
    const lines = [];
    if (dr.problem) lines.push(`<div class="ds-line"><span class="ds-tag">问题</span><span class="ds-text">${escHtml(dr.problem)}</span></div>`);
    if (dr.why_now) lines.push(`<div class="ds-line"><span class="ds-tag">为什么现在</span><span class="ds-text">${escHtml(dr.why_now)}</span></div>`);
    if (dr.approach) lines.push(`<div class="ds-line"><span class="ds-tag">做法</span><span class="ds-text">${escHtml(dr.approach)}</span></div>`);
    if (dr.result) lines.push(`<div class="ds-line"><span class="ds-tag">结果</span><span class="ds-text">${escHtml(dr.result)}</span></div>`);
    return lines.join("");
  }

  function buildLinks(url, pdf){
    let s = "";
    if (url) s += `<a class="link main" href="${escHtml(url)}" target="_blank" rel="noopener">arXiv 摘要</a>`;
    if (pdf) s += `<a class="link" href="${escHtml(pdf)}" target="_blank" rel="noopener">PDF</a>`;
    return s;
  }

  function buildLesson(fl){
    if (!fl.length) return "";
    return `
      <details class="lesson">
        <summary>先补一课 · ${fl.length} 个概念可能需要补一下</summary>
        <div class="lesson-body">
          ${fl.map(f => `
            <div class="lesson-item">
              <div class="lesson-concept">${escHtml(f.concept)}</div>
              <div class="lesson-explain">${escHtml(f.explain)}</div>
              ${f.compiler_link ? `<div class="lesson-link">与编译器的关系：${escHtml(f.compiler_link)}</div>` : ""}
            </div>`).join("")}
        </div>
      </details>`;
  }

  function render(dir){
    const idx = Math.max(0, pages.findIndex(p => p.date === currentDate));
    const page = pages[idx];
    if (!page){
      listEl.innerHTML = "";
      emptyEl.innerHTML = `${LOGO_MARK}<p class="big">书页还是空的</p><p class="small">没有读到任何日报数据。</p>`;
      emptyEl.classList.remove("hidden");
      metaEl.textContent = "";
      return;
    }
    currentDate = page.date;
    const recs = page.recommendations || [];
    const pt = parts(page.date);
    $("folioYear").textContent = pt ? `${pt.y} 年 · 星期${pt.wk}` : "";
    $("folioNum").textContent = pt ? pt.d : page.date;
    $("folioMon").textContent = pt ? `${pt.mo} 月` : "";
    document.title = `${fmtShort(page.date)} · arXiv 日报 · 紫璃`;

    const failed = page.status === "failed" || page.delivery_status === "failed";
    const okish = ["sent", "skipped_no_email", "skipped_empty"].includes(page.delivery_status) || page.status === "complete";
    const bits = [];
    if (page.retrieved_count != null) bits.push(`检索 ${escHtml(page.retrieved_count)}`);
    if (page.candidate_count != null) bits.push(`候选 ${escHtml(page.candidate_count)}`);
    bits.push(`推荐 ${recs.length}`);
    let stat = bits.join('<span class="dot">·</span>');
    if (failed || !okish) stat += `<span class="dot">·</span><span class="warn">状态异常（${escHtml(page.delivery_status || page.status || "未知")}）</span>`;
    metaEl.innerHTML = stat;

    if (recs.length){
      emptyEl.classList.add("hidden");
      listEl.innerHTML = recs.map(entryHtml).join("");
    } else {
      listEl.innerHTML = "";
      const big = failed ? "这一页没能写成" : "这一页是空白的";
      const small = failed ? "当天生成出了点问题，所以留白。" : "今天没有新论文值得翻开，<br>或者这天没有运行。";
      emptyEl.innerHTML = `${LOGO_MARK}<p class="big">${big}</p><p class="small">${small}</p>`;
      emptyEl.classList.remove("hidden");
    }

    dateSelect.value = page.date;
    latestBtn.disabled = idx === 0;
    navBtns.prev.forEach(b => b.disabled = idx >= pages.length - 1);   // 更早 = 列表后面
    navBtns.next.forEach(b => b.disabled = idx <= 0);
    $("pageNo").textContent = $("pageNo2").textContent = `${pages.length - idx} / ${pages.length}`;

    pageEl.classList.remove("turn-next", "turn-prev");
    if (dir){ void pageEl.offsetWidth; pageEl.classList.add(dir > 0 ? "turn-next" : "turn-prev"); }
    try { history.replaceState(null, "", idx === 0 ? location.pathname + location.search : "#" + page.date); } catch (e) {}
  }

  function go(date, dir){
    currentDate = date;
    render(dir);
    window.scrollTo(0, 0);
  }
  function step(delta){          // delta>0：后一天（更新）；delta<0：前一天（更早）
    const idx = pages.findIndex(p => p.date === currentDate);
    const to = idx - delta;
    if (to < 0 || to >= pages.length) return;
    go(pages[to].date, delta);
  }

  navBtns.prev.forEach(b => b.addEventListener("click", () => step(-1)));
  navBtns.next.forEach(b => b.addEventListener("click", () => step(1)));
  dateSelect.addEventListener("change", () => {
    const a = pages.findIndex(p => p.date === currentDate), b = pages.findIndex(p => p.date === dateSelect.value);
    go(dateSelect.value, a > b ? 1 : -1);
  });
  latestBtn.addEventListener("click", () => { if (pages[0]) go(pages[0].date, 1); });
  document.addEventListener("keydown", e => {
    if (e.altKey || e.ctrlKey || e.metaKey || /^(SELECT|INPUT|TEXTAREA)$/.test((e.target.tagName || ""))) return;
    if (e.key === "ArrowLeft") step(-1); else if (e.key === "ArrowRight") step(1);
  });
  let tx = null, ty = null;
  document.addEventListener("touchstart", e => { const t = e.touches[0]; tx = t.clientX; ty = t.clientY; }, { passive: true });
  document.addEventListener("touchend", e => {
    if (tx == null) return;
    const t = e.changedTouches[0], dx = t.clientX - tx, dy = t.clientY - ty; tx = ty = null;
    if (Math.abs(dx) > 70 && Math.abs(dy) < 40) step(dx < 0 ? 1 : -1);   // 左滑 → 更新的一页
  }, { passive: true });
  window.addEventListener("hashchange", () => {
    const h = location.hash.slice(1);
    if (pages.some(p => p.date === h)) go(h, 0);
  });

  fetch("data/daily-index.json")
    .then(r => { if (!r.ok) throw new Error("无法加载索引"); return r.json(); })
    .then(data => {
      pages = (Array.isArray(data) ? data : []).slice().sort((a, b) => String(b.date).localeCompare(String(a.date)));
      renderSelector();
      const h = location.hash.slice(1);
      currentDate = pages.some(p => p.date === h) ? h : (pages[0] ? pages[0].date : null);
      render(0);
    })
    .catch(err => { metaEl.textContent = "加载失败：" + err.message; console.error(err); });
})();
</script>
</body>
</html>"""



def render_html() -> str:
    mark = logo_svg_inline().replace('class="logo"', 'class="mark"').replace('width="26" height="26"', 'width="34" height="34"')
    return (
        HTML_TEMPLATE.replace("__FAVICON__", favicon_href())
        .replace("__LOGO_JS__", json.dumps(mark))
        .replace("__LOGO__", logo_svg_inline())
    )


def write_site(output_dir: Path, pages: list[dict], copy_records: list[dict]) -> None:
    output_dir = Path(output_dir)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # index.html
    (output_dir / "index.html").write_text(render_html(), encoding="utf-8")

    # daily index (list of dates). Include recommendations so the frontend can
    # render a date directly from the index without an extra per-date request.
    index = [
        {
            "date": p["date"],
            "status": p["status"],
            "retrieved_count": p["retrieved_count"],
            "candidate_count": p["candidate_count"],
            "recommendation_count": p["recommendation_count"],
            "delivery_status": p["delivery_status"],
            "recommendations": p["recommendations"],
        }
        for p in pages
    ]
    (data_dir / "daily-index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # per-date records
    for p in pages:
        (data_dir / f"{p['date']}.json").write_text(
            json.dumps(p, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate static paper-radar site")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE), help="Directory with data/daily/*.json")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output site directory")
    args = ap.parse_args()

    source_dir = Path(args.source)
    output_dir = Path(args.output)
    if not source_dir.is_dir():
        raise SystemExit(f"Source dir not found: {source_dir}")

    records = load_records(source_dir)
    if not records:
        raise SystemExit(f"No daily records found in {source_dir}")

    pages = build_page_data(records)
    write_site(output_dir, pages, records)
    print(f"Generated site at {output_dir} ({len(pages)} dates, index.html + data/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
