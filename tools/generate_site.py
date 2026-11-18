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


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>arXiv 日报 · 紫璃</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%20100%20100'%3E%3Crect%20x='12'%20y='12'%20width='76'%20height='76'%20rx='7'%20fill='none'%20stroke='%232563eb'%20stroke-width='3'%20/%3E%3Cpath%20d='M12,12%20h26%20v26'%20fill='none'%20stroke='%232563eb'%20stroke-width='3'%20stroke-linejoin='bevel'%20/%3E%3Cpath%20d='M38,12%20L12,38'%20fill='%23eef2ff'%20stroke='%232563eb'%20stroke-width='3'%20/%3E%3Cpath%20d='M26,40%20h42%20v3%20M26,48%20h34%20v3%20M26,56%20h26%20v3'%20fill='none'%20stroke='%2394a3b8'%20stroke-width='3'%20stroke-linecap='round'%20/%3E%3C/svg%3E">
<style>
:root{
  --bg:#fafbfd; --card:#ffffff; --text:#1f2937; --muted:#6b7280;
  --border:#e5e7eb; --accent:#2563eb; --accent-hover:#1d4ed8;
  --chip:#eff6ff; --chip-text:#1e40af; --shadow:0 4px 16px rgba(0,0,0,0.06);
  --radius: 14px;
}
[data-theme="dark"]{
  --bg:#0b1120; --card:#121a2b; --text:#f1f5f9; --muted:#94a3b8;
  --border:#1f2a44; --accent:#60a5fa; --accent-hover:#93c5fd;
  --chip:#173252; --chip-text:#bfdbfe; --shadow:0 4px 16px rgba(0,0,0,0.45);
}
* { box-sizing: border-box; }
html, body { margin:0; padding:0; background:var(--bg); color:var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", "Noto Sans SC", sans-serif;
  line-height: 1.5; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

header{
  position: sticky; top:0; background: var(--bg); border-bottom: 1px solid var(--border);
  z-index: 50; backdrop-filter: saturate(180%) blur(8px);
}
.header-wrap{
  max-width: 720px; margin: 0 auto; padding: 12px 16px; display: flex;
  align-items: center; justify-content: space-between; gap: 12px;
}
.brand{ display:flex; align-items:center; gap:10px; font-weight: 700; font-size: 1.18rem; letter-spacing: -0.01em; }
.brand .emoji{ font-size: 1.45rem; line-height: 1; }
.titlebar{ font-size: 0.94rem; color: var(--muted); font-weight: 600; }
.subtitle{ font-size: 0.8rem; color: var(--muted); }

main{ max-width: 720px; margin: 0 auto; padding: 20px 16px 90px; }

.toolbar{ display:flex; align-items:center; gap:10px; margin-bottom: 16px;
  flex-wrap: wrap; }
.date-pick{ flex:1; min-width: 160px; }
select{ width:100%; padding: 8px 10px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--card); color: var(--text); font-size: 0.95rem; cursor: pointer; }
.btn-theme{ background: var(--card); border: 1px solid var(--border); color: var(--text);
  border-radius: 10px; padding: 8px 10px; cursor: pointer; font-size: 0.92rem; }
.btn-theme:hover{ background: var(--chip); }

.meta{ color: var(--muted); font-size: 0.87rem; margin-bottom: 8px; }
.meta .ok{ color: #15803d; font-weight: 700; }
.meta .bad{ color: #b91c1c; font-weight: 700; }

.paper-card{ background: var(--card); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 16px 18px; margin-bottom: 14px; box-shadow: var(--shadow);
  transition: transform .12s ease, box-shadow .12s ease, border-color .12s ease; }
.paper-card:hover{ transform: translateY(-2px); box-shadow: 0 10px 28px rgba(0,0,0,0.08); border-color: var(--accent); }
[data-theme="dark"] .paper-card:hover{ box-shadow: 0 10px 28px rgba(0,0,0,0.5); }
.paper-card h2{ margin: 0 0 7px 0; font-size: 1.14rem; font-weight: 700; line-height: 1.4; letter-spacing: -0.01em; }
.paper-card h2 a{ color: var(--text); }
.paper-card h2 a:hover{ color: var(--accent); }
.paper-meta{ font-size: 0.88rem; color: var(--muted); margin-bottom: 8px; display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.chip{ display:inline-flex; align-items:center; gap:4px; padding: 2.5px 9px; border-radius: 11px;
  background: var(--chip); color: var(--chip-text); font-size: 0.73rem; font-weight: 700; }
.tldr{ margin: 9px 0 3px 0; font-size: 1.02rem; color: var(--text); }
.tldr .label{ color: var(--muted); font-size: 0.82rem; font-weight: 700; }
.whycare{ margin: 9px 0 3px 0; font-size: 0.97rem; color: var(--text); }
.whycare .label{ color: var(--muted); font-size: 0.82rem; font-weight: 700; }
.links{ margin-top: 12px; display:flex; gap: 8px; flex-wrap: wrap; }
.link-btn{ display:inline-flex; align-items:center; gap:6px; padding: 7px 14px; border-radius: 10px;
  font-size: 0.9rem; font-weight: 700; border: 1px solid var(--border);
  background: var(--bg); color: var(--text); }
.link-btn.arxiv{ background: var(--accent); color: #fff; border-color: var(--accent);
  box-shadow: 0 1px 3px rgba(37,99,235,0.35); }
.link-btn.arxiv:hover{ background: var(--accent-hover); box-shadow: 0 2px 6px rgba(37,99,235,0.45); }
.link-btn:hover:not(.arxiv){ background: var(--chip); }

.empty{ text-align:center; padding: 44px 16px; color: var(--muted); font-size: 1rem; }
.footer{ color: var(--muted); font-size: 0.78rem; text-align:center; padding: 30px 16px 14px; border-top: 1px solid var(--border); }

.hidden{ display:none !important; }

@media (max-width: 480px){
  main{ padding: 14px 12px 80px; }
  .paper-card{ padding: 14px 16px; border-radius: 12px; }
  .paper-card h2{ font-size: 1.08rem; }
  .header-wrap{ padding: 10px 12px; }
  .brand{ font-size: 1.1rem; }
  .titlebar{ font-size: 0.9rem; }
}
</style>
</head>
<body>
<header>
  <div class="header-wrap">
    <div class="brand"><span class="emoji">🧊</span><div><div class="titlebar">arXiv 日报</div><div class="subtitle">按兴趣推荐的新论文 · 供 tailnet 自用</div></div></div>
    <button class="btn-theme" id="themeToggle" title="切换深色/浅色">🌓</button>
  </div>
</header>
<main>
  <div class="toolbar">
    <div class="date-pick">
      <select id="dateSelect"></select>
    </div>
    <button class="btn-theme" id="latestBtn">最新</button>
  </div>
  <div class="meta" id="meta"></div>
  <div id="list"></div>
  <div class="empty hidden" id="empty">这天还没有推荐记录。</div>
  <div class="footer">
    紫璃的 arXiv 日报 · 数据来自本地运行，仅含论文元数据与摘要，不含任何密钥或个人邮箱。
  </div>
</main>
<script>
(() => {
  const themeKey = "zp:theme";
  const root = document.documentElement;
  const themeBtn = document.getElementById("themeToggle");
  const saved = localStorage.getItem(themeKey);
  const systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const wantDark = saved ? saved === "dark" : systemDark;
  if (wantDark) root.setAttribute("data-theme", "dark");
  themeBtn.addEventListener("click", () => {
    const dark = root.getAttribute("data-theme") === "dark";
    if (dark) root.removeAttribute("data-theme"); else root.setAttribute("data-theme", "dark");
    localStorage.setItem(themeKey, dark ? "light" : "dark");
  });

  const dateSelect = document.getElementById("dateSelect");
  const latestBtn = document.getElementById("latestBtn");
  const metaEl = document.getElementById("meta");
  const listEl = document.getElementById("list");
  const emptyEl = document.getElementById("empty");

  let pages = [];
  let currentDate = null;

  function esc(t){ return String(t == null ? "" : t); }
  function escHtml(t){
    return String(t == null ? "" : t)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
      .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
  }

  function fmtDate(iso){
    if (!iso) return "";
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("zh-CN", { year:"numeric", month:"long", day:"numeric" });
  }

  function renderDateSelector(){
    dateSelect.innerHTML = "";
    for (const p of pages){
      const o = document.createElement("option");
      o.value = p.date;
      o.textContent = fmtDate(p.date) + (p.date === pages[0].date ? " （最新）" : "");
      dateSelect.appendChild(o);
    }
    dateSelect.value = pages[0] ? pages[0].date : "";
  }

  function render(){
    const page = pages.find(p => p.date === currentDate) || pages[0];
    if (!page){ listEl.innerHTML = ""; emptyEl.classList.remove("hidden"); return; }

    emptyEl.classList.add("hidden");
    const recs = page.recommendations || [];
    const statusOk = page.delivery_status === "sent" || page.delivery_status === "skipped_no_email" || page.delivery_status === "skipped_empty" || page.status === "complete";

    metaEl.innerHTML = `
      <strong>${escHtml(fmtDate(page.date))}</strong>
      ${statusOk ? '<span class="ok">· 已生成</span>' : '<span class="bad">· 状态异常 (' + escHtml(page.delivery_status || page.status) + ')</span>'}
      · 检索 ${escHtml(page.retrieved_count ?? "-")} 篇 / 候选 ${escHtml(page.candidate_count ?? "-")} 篇 / 推荐 ${escHtml(recs.length)} 篇
    `;

    let htmlOut = "";
    for (const r of recs){
      const cats = (r.categories || []).map(c => `<span class="chip">${escHtml(c)}</span>`).join("");
      const bucket = r.bucket ? `<span class="chip">${escHtml(r.bucket)}</span>` : "";
      const rel = r.relevance != null ? `<span class="chip">相关性 ${r.relevance}/10</span>` : "";
      const pub = r.published_at ? `· 发表 ${escHtml(r.published_at)}` : "";
      htmlOut += `
        <div class="paper-card">
          <h2><a href="${escHtml(r.url)}" target="_blank" rel="noopener">${escHtml(r.title)}</a></h2>
          <div class="paper-meta">
            <span>作者：${escHtml(r.authors)}</span>
            ${pub}
            ${cats}${bucket}${rel}
          </div>
          <div class="tldr"><span class="label">一句 TLDR（中文）：</span>${escHtml(r.tldr_zh || "(无摘要)")}</div>
          ${r.why_care_zh ? `<div class="whycare"><span class="label">为何推荐：</span>${escHtml(r.why_care_zh)}</div>` : ""}
          <div class="links">
            <a class="link-btn arxiv" href="${escHtml(r.url)}" target="_blank" rel="noopener">arXiv 摘要</a>
            ${r.pdf_url ? `<a class="link-btn" href="${escHtml(r.pdf_url)}" target="_blank" rel="noopener">PDF</a>` : ""}
          </div>
        </div>`;
    }
    if (!recs.length) htmlOut = "<div class='empty'>当天没有推荐论文。</div>";
    listEl.innerHTML = htmlOut;
  }

  fetch("data/daily-index.json")
    .then(r => { if (!r.ok) throw new Error("无法加载索引"); return r.json(); })
    .then(data => { pages = data; renderDateSelector(); currentDate = pages[0] ? pages[0].date : null; render(); })
    .catch(err => { metaEl.textContent = "加载失败：" + esc(err.message); console.error(err); });

  dateSelect.addEventListener("change", () => { currentDate = dateSelect.value; render(); });
  latestBtn.addEventListener("click", () => { currentDate = pages[0] ? pages[0].date : null; dateSelect.value = currentDate || ""; render(); });
})();
</script>
</body>
</html>"""


def write_site(output_dir: Path, pages: list[dict], copy_records: list[dict]) -> None:
    output_dir = Path(output_dir)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # index.html
    (output_dir / "index.html").write_text(HTML_TEMPLATE, encoding="utf-8")

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
