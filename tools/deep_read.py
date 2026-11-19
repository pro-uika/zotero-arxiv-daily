#!/usr/bin/env python3
"""tools/deep_read.py —— 为已有每日记录补生成“深读”结构化字段。

对每篇推荐论文：
  1. 尽量抓取 arXiv 正文引言（https://arxiv.org/html/<id>/），失败则仅据摘要；
  2. 调用 OFM 网关 LLM 生成背景/问题、做法、结果、局限，以及“先补一课”（最多 3 个概念）；
  3. “先补一课”读取 knowledge/profile.md：“熟”不讲、“半懂”讲到点上、“已讲过”不重复；
  4. 新讲过的概念追加到 profile.md“已讲过”，并保持总字数上限（1500 字）；
  5. 将 deep_read 字段写回 data/daily/YYYY-MM-DD.json，供 generate_site.py 渲染。

用法：
  python tools/deep_read.py data/daily/2026-11-17.json data/daily/2026-11-18.json
  python tools/deep_read.py --all        # 处理最新的一份每日记录

设计：
  - 凭据只读：OFM_API_KEY 来自 /home/node/.dsh/.credentials.yaml，不打印、不入仓库；
  - 并发受控（默认 2），失败重试；内容诚实，拿不到正文就只基于摘要并标注“仅据摘要”；
  - 全部简体中文，术语保留英文。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = REPO / "knowledge" / "profile.md"
DAILY_DIR = REPO / "data" / "daily"

OFM_BASE_URL = "http://127.0.0.1:18900/v1"
OFM_MODEL = "deepseek-v4.1-flash-free"
CREDENTIALS = Path("/home/node/.dsh/.credentials.yaml")
PROFILE_CHAR_LIMIT = 1500
MAX_WORKERS = 2
MAX_RETRIES = 3


# --------------------------------------------------------------------------- #
# 凭据与 profile
# --------------------------------------------------------------------------- #
def load_ofm_key() -> str:
    text = CREDENTIALS.read_text(encoding="utf-8")
    m = re.search(r"^\s*OFM_API_KEY:\s*(\S+)$", text, flags=re.M)
    if not m:
        raise RuntimeError("OFM_API_KEY not found in " + str(CREDENTIALS))
    return m.group(1)


def _section(md: str, name: str) -> str:
    """提取 markdown 中某个 ## 一级标题下直到下一个 ## 之前的文本块。"""
    pattern = re.compile(r"^##\s+" + re.escape(name) + r"\b(.*?)(?=^##\s|\Z)", flags=re.S | re.M)
    m = pattern.search(md)
    return m.group(1).strip() if m else ""


def load_profile(path: Path = DEFAULT_PROFILE) -> dict[str, str]:
    md = path.read_text(encoding="utf-8") if path.exists() else ""
    return {
        "熟": _section(md, "熟"),
        "半懂": _section(md, "半懂"),
        "不懂": _section(md, "不懂"),
        "已讲过": _section(md, "已讲过的概念"),
    }


def profile_total_chars(path: Path = DEFAULT_PROFILE) -> int:
    return len(path.read_text(encoding="utf-8")) if path.exists() else 0


def append_taught(concepts: list[str], path: Path = DEFAULT_PROFILE) -> None:
    """把新讲过的概念追加到“已讲过的概念”，并保持总字数上限。"""
    if not concepts:
        return
    md = path.read_text(encoding="utf-8") if path.exists() else ""
    # 去重（不重复已讲过）
    existing = _section(md, "已讲过的概念")
    existing_set = {c.strip(" -•\t") for c in existing.splitlines() if c.strip()}
    new_lines = []
    for c in concepts:
        c = c.strip(" -•\t")
        if c and c not in existing_set:
            new_lines.append(f"- {c}")
            existing_set.add(c)
    if not new_lines:
        return
    block = "## 已讲过的概念（不再重复讲）\n"
    if existing.strip():
        block = existing.rstrip() + "\n" + "\n".join(new_lines) + "\n"
    else:
        block = block + "\n".join(new_lines) + "\n"
    md2 = re.sub(
        r"^##\s+已讲过的概念\b(.*?)(?=^##\s|\Z)",
        block,
        md,
        flags=re.S | re.M,
        count=1,
    )
    if len(md2) > PROFILE_CHAR_LIMIT:
        # 从前往后裁剪已讲过条目，直到满足上限
        lines = md2.splitlines(keepends=True)
        keep = []
        for line in lines:
            trial = "".join(keep + [line])
            if len(trial) <= PROFILE_CHAR_LIMIT:
                keep.append(line)
            else:
                break
        md2 = "".join(keep)
    path.write_text(md2, encoding="utf-8")


# --------------------------------------------------------------------------- #
# arXiv 正文抓取
# --------------------------------------------------------------------------- #
def _strip_html(html_text: str) -> str:
    html_text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", html_text)
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"&[a-z]+;", lambda m: {
        "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&nbsp;": " ",
    }.get(m.group(0), " "), html_text)
    # 先再 unescape 一遍常见实体
    import html as html_mod
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fetch_arxiv_intro(paper_id: str) -> tuple[str, str]:
    """返回 (正文文本, source)。source ∈ {full_text, abstract_only}。

    优先抓 https://arxiv.org/html/<id>/，提取摘要+引言+结论节选；
    失败则返回 ("", "abstract_only")，由调用方配合摘要使用。
    """
    import html as html_mod

    # 规normalize id：去掉可能的版本后缀 / http 前缀
    pid = paper_id.strip()
    pid = re.sub(r"^https?://arxiv.org/(abs|html)/", "", pid)
    pid = re.sub(r"v\d+$", "", pid)

    url = f"https://arxiv.org/html/{pid}/"
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "research-bot (mailto:example@example.com)"},
        )
        with urllib.request.urlopen(req, timeout=40) as r:
            raw = r.read()
        html_text = raw.decode("utf-8", errors="replace")
    except Exception:
        return "", "abstract_only"

    text = _strip_html(html_text)
    if not text:
        return "", "abstract_only"

    parts = []

    # 摘要：从 "Abstract." 到 "1 Introduction"
    m = re.search(r"Abstract\.\s*(.*?)\s*1\s+Introduction", text)
    if m:
        parts.append("[摘要]\n" + m.group(1).strip())
    else:
        # 兜底：取开头一段作为摘要
        parts.append("[摘要]\n" + text[:600])

    # 引言：从 "1 Introduction" 到 "2 " 下一节
    m = re.search(r"1\s+Introduction\s*(.*?)(?=\n\s*2\s+\S|\n\s*[2-9]\s+\S)", text)
    if m:
        parts.append("[引言]\n" + m.group(1).strip()[:7000])

    # 结论/未来工作：用于结果与局限
    m = re.search(r"(Conclusion and Future Work|Conclusion)\s*(.*?)(?=Acknowledgements|References)", text)
    if m:
        parts.append("[结论]\n" + m.group(2).strip()[:3000])

    combined = "\n".join(parts)
    combined = re.sub(r"\s+", " ", combined).strip()
    if not combined:
        return "", "abstract_only"
    return combined[:16000], "full_text"


# --------------------------------------------------------------------------- #
# LLM 调用
# --------------------------------------------------------------------------- #
def build_prompt(title: str, authors: str, abstract: str, intro_text: str,
                 profile: dict[str, str]) -> str:
    return f"""你是一位擅长给编译体系方向博士读 arXiv 论文的助手。请基于提供的论文标题、作者、摘要与引言正文（如仅提供摘要，则据摘要如实生成，不得编造论文中没有的细节），输出一个 JSON 对象，字段：
- problem：这篇论文在解决什么具体问题（1 句）。
- why_now：为什么现在值得做（1 句）。
- approach：核心做法（1-2 句）。
- result：主要结果（1-2 句）。
- limitation：局限性（1-2 句；文中未明确展开则写“文中未明确展开”）。
- source：取 "full_text"（用到了正文）或 "abstract_only"（仅据摘要）。
- first_lesson：最多 3 个用户可能不熟悉、又有助于理解本文的概念。每个为 {{concept, explain, compiler_link}}，explain 用 1-2 句简体中文，compiler_link 点明与编译器研究的关系。

概念选择规则（严格遵守）：
- 只能从“半懂”或“不懂”名单里选，且必须与本文理解相关。
- 不得选“熟”名单里的概念；不得重复“已讲过”名单里的概念。
- explain 里术语保留英文，其余用简体中文。

已知用户画像（按类别）：
【熟】{profile['熟']}
【半懂】{profile['半懂']}
【不懂】{profile['不懂']}
【已讲过】{profile['已讲过']}

论文：
标题：{title}
作者：{authors}
摘要：{abstract}
正文（引言/结论节选）：{intro_text}

只输出 JSON，不要包含代码块标记或额外说明。"""


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def llm_deep_read(api_key: str, title: str, authors: str, abstract: str,
                  intro_text: str, source: str, profile: dict[str, str]) -> dict[str, Any] | None:
    prompt = build_prompt(title, authors, abstract, intro_text, profile)
    body = json.dumps({
        "model": OFM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.2,
    }).encode("utf-8")
    req = urllib.request.Request(
        OFM_BASE_URL + "/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )
    last_err = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            if not content:
                raise ValueError("LLM 返回空内容")
            result = _extract_json(content)
            if result is None:
                raise ValueError("LLM 返回未含有效 JSON")
            result["source"] = source
            # 规范 first_lesson
            fl = result.get("first_lesson") or []
            if not isinstance(fl, list):
                fl = []
            clean = []
            for item in fl[:3]:
                if not isinstance(item, dict):
                    continue
                clean.append({
                    "concept": str(item.get("concept", "")).strip(),
                    "explain": str(item.get("explain", "")).strip(),
                    "compiler_link": str(item.get("compiler_link", "")).strip(),
                })
            result["first_lesson"] = clean
            return result
        except Exception as exc:
            last_err = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(2 * attempt)
    print(f"[deep_read] LLM 失败（{title}）：{last_err}")
    return None


# --------------------------------------------------------------------------- #
# 单篇处理
# --------------------------------------------------------------------------- #
def process_paper(api_key: str, rec: dict[str, Any], profile: dict[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
    """为一篇论文生成 deep_read，返回 (deep_read 或 None, 新讲过的概念)。"""
    paper_id = rec.get("paper_id") or ""
    title = rec.get("title") or ""
    authors = "、".join(rec.get("authors") or []) if isinstance(rec.get("authors"), list) else str(rec.get("authors") or "")
    abstract = rec.get("abstract") or ""

    intro_text, source = fetch_arxiv_intro(paper_id)
    if source == "abstract_only":
        intro_text = abstract

    result = llm_deep_read(api_key, title, authors, abstract, intro_text, source, profile)
    taught: list[str] = []
    if result is not None:
        taught = [item.get("concept", "") for item in (result.get("first_lesson") or []) if item.get("concept")]
    return result, taught


def process_file(path: Path, api_key: str, profile: dict[str, str]) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    recs = record.get("recommendations") or []
    taught_all: list[str] = []

    def work(rec):
        if rec.get("deep_read"):
            return rec, []
        result, taught = process_paper(api_key, rec, profile)
        if result is not None:
            rec = dict(rec)
            rec["deep_read"] = result
        return rec, taught

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = [ex.submit(work, r) for r in recs]
        for i, fut in enumerate(as_completed(futures)):
            rec, taught = fut.result()
            recs[i] = rec
            taught_all.extend(taught)

    record["recommendations"] = recs
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"file": str(path), "n": len(recs), "taught": taught_all}


def main(argv: list[str] | None = None) -> int:
    import sys
    argv = sys.argv[1:] if argv is None else argv

    api_key = load_ofm_key()
    profile = load_profile()

    files: list[Path] = []
    if "--all" in argv:
        files = sorted(DAILY_DIR.glob("*.json"))
        if not files:
            print("没有找到 data/daily/*.json")
            return 1
        files = [files[-1]]
    else:
        files = [Path(p) for p in argv if p.endswith(".json")]
    if not files:
        print("用法：python tools/deep_read.py data/daily/2026-11-17.json ...  (或 --all)")
        return 1

    summary = []
    for f in files:
        if not f.exists():
            print(f"[跳过] 不存在：{f}")
            continue
        res = process_file(f, api_key, profile)
        summary.append(res)
        print(f"[deep_read] {res['file']}：{res['n']} 篇，新讲过概念：{res['taught']}")

    # 把新讲过的概念写回 profile
    all_taught = [c for res in summary for c in res["taught"]]
    append_taught(all_taught)
    print(f"[profile] 已讲过新增：{all_taught}，总字数 {profile_total_chars()} / {PROFILE_CHAR_LIMIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
