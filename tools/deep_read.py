#!/usr/bin/env python3
"""tools/deep_read.py —— 为已有每日记录补生成“深读”结构化字段（中英双语）。

对每篇推荐论文：
  1. 尽量抓取 arXiv 正文引言（https://arxiv.org/html/<id>/），失败则仅据摘要；
  2. First pass（英文）：用英文 prompt 生成纯英文的 problem / why_now / approach / result
     四句 + 2-3 个具体概念（term、explanation 1-2 句、compiler_link），规则与旧版一致；
  3. Second pass（中文）：单独再调一次模型，把 first pass 的英文结果翻译成流畅简体中文，
     并把标题、摘要、正文前若干字符作为术语与语境依据放进 prompt；专业术语在中文句子
     里保留英文原词，其余自然中文，不逐字硬翻、不增删事实；
  4. “先补一课”读取 knowledge/profile.md：“熟”不讲、“半懂”讲到点上、“已讲过”不重复；
  5. 新讲过的概念追加到 profile.md“已讲过”，并保持总字数上限（1500 字）；
  6. 将 deep_read.en / deep_read.zh 写回 data/daily/YYYY-MM-DD.json，供 generate_site.py 渲染。

旧数据（只有扁平字段 problem/why_now/approach/result/...）被视为 zh，页面仍能显示。

用法：
  python tools/deep_read.py data/daily/2026-11-17.json data/daily/2026-11-18.json --force
  python tools/deep_read.py --all        # 处理最新的一份每日记录

设计：
  - 凭据只读：OFM_API_KEY 来自 /home/node/.dsh/.credentials.yaml，不打印、不入仓库；
  - 并发受控（默认 2），失败重试、间隔逐次拉长；仍失败就保留旧结果；
  - 内容诚实，拿不到正文就只基于摘要并标注“仅据摘要”；
  - first pass 全英文，second pass 译成简体中文。
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
MAX_RETRIES = 6
# second pass 翻译时，作为语境依据的正文字符上限（复用现有全文截取逻辑）
ZH_CONTEXT_CHARS = 7000

# 严禁作为“先补一课”概念的过宽泛大类词（含 profile 里“半懂”的 Zotero 大类主题）。
# 只讲论文里出现的具体术语/技术，不讲这些大类。
BROAD_TERMS = {
    "ai/ml", "ai", "ml", "人工智能", "机器学习", "深度学习", "ai 系统", "ai系统",
    "编译器", "计算机架构", "计算机体系结构", "体系结构", "并行计算", "量化",
    "ai 系统（zotero：ai system）", "ai/ml（zotero：ai）", "编译器（zotero：compiler）",
    "计算机架构（zotero：arch）", "材料/系统（zotero：material）",
}


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
# LLM 调用（通用，带退避重试）
# --------------------------------------------------------------------------- #
def _call_llm(api_key: str, prompt: str, max_tokens: int = 1500) -> str | None:
    """调一次 OFM 网关，返回模型文本；失败返回 None。"""
    body = json.dumps({
        "model": OFM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
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
            return content
        except Exception as exc:
            last_err = str(exc)
            if attempt < MAX_RETRIES:
                time.sleep(5 * attempt)
    print(f"[deep_read] LLM 多次失败：{last_err}")
    return None


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s.strip().lower())


def filter_concepts(lessons: list[dict[str, str]], profile: dict[str, str]) -> list[dict[str, str]]:
    """过滤第一课概念：剔除宽泛大类、已讲过/熟的概念，保留 2-3 个具体术语。"""
    taught = _norm(profile.get("已讲过", ""))
    skilled = _norm(profile.get("熟", ""))
    taught_terms = [t for t in taught.split("\n") if t]
    skilled_terms = [t for t in skilled.split("\n") if t]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in lessons:
        concept = str(item.get("concept", "")).strip()
        if not concept:
            continue
        key = _norm(concept)
        if key in seen:
            continue
        # 剔除宽泛大类词
        if any(term and term in key for term in BROAD_TERMS):
            continue
        # 剔除“熟”或“已讲过”里已有的概念（子串匹配，防止重复讲）
        if key and (key in taught or key in skilled
                    or any(t and t in key for t in taught_terms)
                    or any(t and t in key for t in skilled_terms)):
            continue
        seen.add(key)
        out.append({
            "concept": concept,
            "explain": str(item.get("explain", "")).strip(),
            "compiler_link": str(item.get("compiler_link", "")).strip(),
        })
        if len(out) >= 3:
            break
    return out


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
def build_prompt_en(title: str, authors: str, abstract: str, intro_text: str,
                    profile: dict[str, str]) -> str:
    """First pass：英文，产出纯英文的 four sentences + first_lesson。"""
    return f"""You are an assistant skilled at helping a UIUC compiler PhD read arXiv papers. Based on the paper title, authors, abstract, and full-text introduction (if only the abstract is available, generate faithfully from the abstract without inventing details not in the paper), output a JSON object with these fields:
- problem: the specific problem this paper solves (1 sentence).
- why_now: why it is worth doing now (1 sentence).
- approach: the core approach (1-2 sentences).
- result: the main results (1-2 sentences).
- limitation: limitations (1-2 sentences; write "Not explicitly discussed in the paper" if not clearly expanded).
- source: "full_text" (full text was used) or "abstract_only" (only the abstract).
- first_lesson: at most 3 concepts the user may not know yet that help understand the paper. Each is {{concept, explain, compiler_link}}; explain in 1-2 English sentences, compiler_link points to the relationship with compiler research.

Terminology rules (strict):
- Keep professional terms in their English original form (e.g. scheduling language, bank conflict, superoptimization, tensor graph, lowering, tiling, fusion, covert channel, timing attack), never translate them into Chinese only.
- Person names, system names, dataset names, and metric names are kept as-is.

Concept selection rules (strict):
- Choose only from specific terms/technologies/system names that appear in the paper's abstract or full text (e.g. Triton, superoptimization, e-graph, equality saturation, backend lowering, fusion).
- Do NOT choose overly broad categories: AI/ML, AI, ML, AI systems, compiler, computer architecture, machine learning, artificial intelligence, deep learning, parallel computing, quantization, etc.
- Do NOT choose concepts in the user's "skilled" list or already in "taught"; "skilled" concepts are not taught.
- 2-3 concepts per paper.

User profile (by category):
[skilled] {profile['熟']}
[learning] {profile['半懂']}
[unknown] {profile['不懂']}
[taught] {profile['已讲过']}

Paper:
Title: {title}
Authors: {authors}
Abstract: {abstract}
Full text (introduction / conclusion excerpt): {intro_text}

Output only JSON, no code block markers or extra explanation."""


def build_prompt_zh(title: str, authors: str, abstract: str, intro_text: str,
                    en_result: dict[str, Any]) -> str:
    """Second pass：把 first pass 的英文结果翻译成流畅的简体中文。"""
    context_intro = intro_text[:ZH_CONTEXT_CHARS] if intro_text else ""
    return f"""You are a careful translator. Translate the following English deep-read result of an arXiv paper into fluent Simplified Chinese for a UIUC compiler PhD.

Terminology and language context (paper title, abstract, and the first part of the full text):
Title: {title}
Authors: {authors}
Abstract: {abstract}
Full text (first part): {context_intro}

English result to translate:
{json.dumps(en_result, ensure_ascii=False, indent=2)}

Translation requirements (strict):
- Professional terms keep their English original form inside Chinese sentences (e.g. scheduling language, bank conflict); do not translate them into Chinese only.
- The rest should be natural Simplified Chinese, not word-for-word translation; do not add or remove facts.
- Keep the same JSON structure: problem, why_now, approach, result, limitation, source, first_lesson.
- In first_lesson, keep `concept` in English (it is a term); translate `explain` and `compiler_link` into natural Chinese while keeping English terms.
- Output only JSON, no code block markers or extra explanation."""


# --------------------------------------------------------------------------- #
# 单篇处理（两 pass）
# --------------------------------------------------------------------------- #
def _two_pass(api_key: str, title: str, authors: str, abstract: str,
              intro_text: str, source: str, profile: dict[str, str]) -> dict[str, Any] | None:
    """先英文 pass，再中文翻译 pass。两 pass 都成功才返回 {en, zh}；否则返回 None（保留旧结果）。"""
    # First pass（英文）
    prompt_en = build_prompt_en(title, authors, abstract, intro_text, profile)
    content_en = _call_llm(api_key, prompt_en)
    if content_en is None:
        return None
    en = _extract_json(content_en)
    if en is None:
        print(f"[deep_read] 第一 pass 返回未含有效 JSON（{title}）")
        return None
    en["source"] = source
    fl = en.get("first_lesson") or []
    if not isinstance(fl, list):
        fl = []
    en["first_lesson"] = filter_concepts(fl, profile)

    # Second pass（中文翻译）
    prompt_zh = build_prompt_zh(title, authors, abstract, intro_text, en)
    content_zh = _call_llm(api_key, prompt_zh)
    if content_zh is None:
        print(f"[deep_read] 第二 pass 翻译失败（{title}）")
        return None
    zh = _extract_json(content_zh)
    if zh is None:
        print(f"[deep_read] 第二 pass 返回未含有效 JSON（{title}）")
        return None
    zh["source"] = source
    fl_zh = zh.get("first_lesson") or []
    if not isinstance(fl_zh, list):
        fl_zh = []
    # 中文 pass 的概念保留英文 term，explain/compiler_link 已译中文；把概念统一对齐到英文 pass 的结果
    zh_first_lesson = []
    for i, item in enumerate(fl_zh):
        concept = str(item.get("concept", "")).strip()
        if not concept and i < len(en["first_lesson"]):
            concept = en["first_lesson"][i].get("concept", "")
        zh_first_lesson.append({
            "concept": concept,
            "explain": str(item.get("explain", "")).strip(),
            "compiler_link": str(item.get("compiler_link", "")).strip(),
        })
    if not zh_first_lesson and en["first_lesson"]:
        zh_first_lesson = [
            {"concept": c.get("concept", ""), "explain": "", "compiler_link": ""}
            for c in en["first_lesson"]
        ]
    zh["first_lesson"] = zh_first_lesson[:3]

    return {"en": en, "zh": zh}


def process_paper(api_key: str, rec: dict[str, Any], profile: dict[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
    """为一篇论文生成 deep_read，返回 (deep_read 或 None, 新讲过的概念)。

    deep_read = {"en": {...}, "zh": {...}}；两 pass 任一失败则返回 None，由调用方保留旧结果。
    """
    paper_id = rec.get("paper_id") or ""
    title = rec.get("title") or ""
    authors = "、".join(rec.get("authors") or []) if isinstance(rec.get("authors"), list) else str(rec.get("authors") or "")
    abstract = rec.get("abstract") or ""

    intro_text, source = fetch_arxiv_intro(paper_id)
    if source == "abstract_only":
        intro_text = abstract

    result = _two_pass(api_key, title, authors, abstract, intro_text, source, profile)
    if result is None and intro_text:
        # 换更短的输入重试（如只用摘要 + 正文前若干字符），对之前返回空的论文更友好
        short_text = (abstract + "\n" + intro_text[:ZH_CONTEXT_CHARS]).strip() or abstract
        if short_text != intro_text:
            result = _two_pass(api_key, title, authors, abstract, short_text, source, profile)

    taught: list[str] = []
    if result is not None:
        taught = [item.get("concept", "") for item in (result["en"].get("first_lesson") or []) if item.get("concept")]
    return result, taught


def process_file(path: Path, api_key: str, profile: dict[str, str], force: bool = False) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    recs = record.get("recommendations") or []
    taught_all: list[str] = []

    def work(rec):
        if rec.get("deep_read") and not force:
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
    force = "--force" in argv
    if "--all" in argv:
        files = sorted(DAILY_DIR.glob("*.json"))
        if not files:
            print("没有找到 data/daily/*.json")
            return 1
        files = [files[-1]]
    else:
        files = [Path(p) for p in argv if p.endswith(".json")]
    if not files:
        print("用法：python tools/deep_read.py data/daily/2026-11-17.json ...  (或 --all，加 --force 强制重生成)")
        return 1

    summary = []
    for f in files:
        if not f.exists():
            print(f"[跳过] 不存在：{f}")
            continue
        res = process_file(f, api_key, profile, force=force)
        summary.append(res)
        print(f"[deep_read] {res['file']}：{res['n']} 篇，新讲过概念：{res['taught']}")

    # 把新讲过的概念写回 profile
    all_taught = [c for res in summary for c in res["taught"]]
    append_taught(all_taught)
    print(f"[profile] 已讲过新增：{all_taught}，总字数 {profile_total_chars()} / {PROFILE_CHAR_LIMIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
