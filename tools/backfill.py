#!/usr/bin/env python3
"""tools/backfill.py —— 回填缺失日期的 arXiv 每日日报。

对每个缺失的 arXiv 发布日（跳过周末），按 arXiv API 的
`cat:<类别> AND submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]` 窗口抓取
历史论文，走与 run_daily 相同的 embedding 排序（本地
http://127.0.0.1:18960/v1，key local，BAAI/bge-m3；GPU 503 等待，不连环重试）
+ LLM 评测（OFM 网关 http://127.0.0.1:18900/v1，模型 combo:deepread，
key 只读 /home/node/.dsh/.credentials.yaml 的 OFM_API_KEY，不打印、不提交），
生成 data/daily/<日期>.json，再用 tools/deep_read.py 深读（--workers 1）。

每天跑完执行 python tools/generate_site.py，并用 curl
http://127.0.0.1:18970/papers/ 验证日期出现。

设计：
  - 不并发多个日期，串行逐日跑；
  - arXiv API 礼貌限速 >= 3 秒/请求，429 时退避，bounded 次数后放弃该日期；
  - 总时限 3 小时，不足时停止；
  - 卡住只杀自己的进程（requests timeout + bounded 退避）；
  - 每天独立：失败的日期不阻塞后续日期。

用法：
  python tools/backfill.py 2026-10-23                    # 只回填一天
  python tools/backfill.py 2026-10-23 2026-10-26 ...    # 指定多天
  python tools/backfill.py --range 2026-10-23 2026-11-16 # 回填区间（自动跳周末、已有日期）
  python tools/backfill.py --all-missing                  # 回填 2026-10-23..2026-11-16 所有缺失平日
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# 线程限制：在任意科学库/ numpy 导入前设置
# --------------------------------------------------------------------------- #
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from omegaconf import OmegaConf  # noqa: E402
import feedparser  # noqa: E402
import requests  # noqa: E402

from zotero_arxiv_daily.executor import Executor  # noqa: E402
from zotero_arxiv_daily.retriever.arxiv_retriever import (  # noqa: E402
    ArxivRetriever,
    _entry_to_arxiv_result,
)
from zotero_arxiv_daily.candidate_generation import CandidateGenerator, filter_by_category  # noqa: E402
from zotero_arxiv_daily import assessment  # noqa: E402
from zotero_arxiv_daily.daily_report import build_daily_record, write_daily_record  # noqa: E402
from zotero_arxiv_daily.protocol import Paper  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_UA = "research-bot/1.0 (mailto:example@example.com)"
ARXIV_MIN_INTERVAL = 3.5  # 秒，arXiv API 礼貌限速 >= 3s
ARXIV_MAX_ATTEMPTS = 4
ARXIV_RATE_SLEEP = 60.0   # 遇到 429 时的退避秒数

OFM_BASE_URL = "http://127.0.0.1:18900/v1"
OFM_MODEL = "combo:deepread"
EMBED_BASE_URL = "http://127.0.0.1:18960/v1"
EMBED_KEY = "local"
EMBED_MODEL = "BAAI/bge-m3"

CREDENTIALS = Path("/home/node/.dsh/.credentials.yaml")
SECRETS_ENV = Path("/workspace/.secrets/zotero.env")
DAILY_DIR = REPO / "data" / "daily"

TOTAL_TIME_LIMIT = 3 * 60 * 60  # 3 小时

# 回填目标窗口：2026-10-23 到 2026-11-16（GitHub Action 被停区间）
BACKFILL_FROM = dt.date(2026, 10, 23)
BACKFILL_TO = dt.date(2026, 11, 16)


# --------------------------------------------------------------------------- #
# 凭据与环境
# --------------------------------------------------------------------------- #
def _load_zotero_env() -> None:
    """把 /workspace/.secrets/zotero.env 中的 ZOTERO_ID/ZOTERO_KEY 载入环境（不打印）。"""
    if not SECRETS_ENV.exists():
        return
    for raw in SECRETS_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def load_ofm_key() -> str:
    text = CREDENTIALS.read_text(encoding="utf-8")
    m = re.search(r"^\s*OFM_API_KEY:\s*(\S+)$", text, flags=re.M)
    if not m:
        raise RuntimeError("OFM_API_KEY not found in " + str(CREDENTIALS))
    return m.group(1)


def _load_config() -> Any:
    _load_zotero_env()
    ofm_key = load_ofm_key()
    os.environ.setdefault("OPENAI_API_KEY", ofm_key)
    os.environ.setdefault("OPENAI_API_BASE", OFM_BASE_URL)

    base = OmegaConf.load(REPO / "config" / "base.yaml")
    custom = OmegaConf.load(REPO / "config" / "custom.yaml")
    config = OmegaConf.merge(base, custom)

    # 回填走 OFM 网关 + 本地 embedding，模型与 run_daily 一致（combo:deepread / bge-m3）
    config.llm.api.base_url = OFM_BASE_URL
    config.llm.api.key = ofm_key
    config.llm.api_mode = "chat_completion"
    config.llm.generation_kwargs.model = OFM_MODEL
    config.executor.reranker = "api"
    config.executor.source = ["arxiv"]
    config.executor.send_email = False
    return config


# --------------------------------------------------------------------------- #
# arXiv API 抓取（submittedDate 历史窗口）
# --------------------------------------------------------------------------- #
def _arxiv_date_window(date_str: str) -> tuple[str, str]:
    """返回 [start, end] arXiv submittedDate 窗口（UTC，包含当天 00:00，不含次日）。"""
    d = dt.date.fromisoformat(date_str)
    start = d.strftime("%Y%m%d0000")
    end = (d + dt.timedelta(days=1)).strftime("%Y%m%d0000")
    return start, end


def fetch_arxiv_for_date(date_str: str, categories: list[str], config: Any) -> list[Paper]:
    """按 arXiv API submittedDate 窗口抓取某天的论文，返回去重后的 Paper 列表。

    每个类别单独查询（cat: 匹配所有声明类别，含交叉列），再按 paper_id 去重。
    礼貌限速 >= 3s/请求；429 时退避，bounded 次数后放弃该类别。
    """
    start, end = _arxiv_date_window(date_str)
    retriever = ArxivRetriever(config)

    seen: dict[str, Any] = {}
    last_request = 0.0

    for cat in categories:
        q = (
            f"cat:{cat}%20AND%20submittedDate:%5B{start}%20TO%20{end}%5D"
        )
        url = f"{ARXIV_API}?search_query={q}&max_results=2000&start=0"

        fetched: list[Paper] = []
        for attempt in range(1, ARXIV_MAX_ATTEMPTS + 1):
            # 礼貌限速
            elapsed = time.monotonic() - last_request
            if elapsed < ARXIV_MIN_INTERVAL:
                time.sleep(ARXIV_MIN_INTERVAL - elapsed)
            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": ARXIV_UA},
                    timeout=(20, 90),
                )
            except requests.RequestException as exc:
                last_request = time.monotonic()
                if attempt < ARXIV_MAX_ATTEMPTS:
                    time.sleep(min(ARXIV_RATE_SLEEP, 10 * attempt))
                    continue
                print(f"[backfill] {date_str} {cat}: 请求异常 {exc}，放弃该类别")
                break

            last_request = time.monotonic()

            if resp.status_code == 429:
                print(f"[backfill] {date_str} {cat}: arXiv 429 限速，等待 {ARXIV_RATE_SLEEP}s 后重试 "
                      f"({attempt}/{ARXIV_MAX_ATTEMPTS})")
                if attempt < ARXIV_MAX_ATTEMPTS:
                    time.sleep(ARXIV_RATE_SLEEP)
                    continue
                print(f"[backfill] {date_str} {cat}: 多次 429，放弃该类别")
                break
            if resp.status_code >= 400:
                print(f"[backfill] {date_str} {cat}: HTTP {resp.status_code}，放弃该类别")
                break

            feed = feedparser.parse(resp.text)
            entries = getattr(feed, "entries", []) or []
            for entry in entries:
                try:
                    result = _entry_to_arxiv_result(entry)
                    paper = retriever.convert_to_paper(result)
                except Exception as exc:
                    print(f"[backfill] {date_str} {cat}: 转换条目失败 {exc}")
                    continue
                pid = paper.paper_id
                if pid and pid not in seen:
                    seen[pid] = paper
                    fetched.append(paper)
            break  # 该类别成功，进入下一类别

        print(f"[backfill] {date_str} {cat}: 抓取 {len(fetched)} 篇（累计去重后 {len(seen)} 篇）")

    eligible = filter_by_category(list(seen.values()), config.get("research", {}))
    print(f"[backfill] {date_str}: 类别门控后剩余 {len(eligible)}/{len(seen)} 篇")
    return eligible, len(seen)


# --------------------------------------------------------------------------- #
# 单日回填流程
# --------------------------------------------------------------------------- #
def backfill_one_date(
    date_str: str,
    config: Any,
    executor: Executor,
    corpus: list[Any],
    recent_papers: list[Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """回填单个日期，返回 {date, status, counts, error}。"""
    t0 = time.monotonic()
    categories = list(config.source.arxiv.category or [])
    print(f"\n=== 回填 {date_str}（抓取 {categories}） ===")

    papers, retrieved_count = fetch_arxiv_for_date(date_str, categories, config)

    if not papers:
        record = build_daily_record(
            report_date=date_str,
            retrieved_count=retrieved_count,
            candidates=[],
            recommendations=[],
            status="complete",
        )
        if not dry_run:
            write_daily_record(record, DAILY_DIR)
        print(f"[backfill] {date_str}: 无合格论文，已写空记录")
        return {"date": date_str, "status": "empty", "retrieved": retrieved_count,
                "candidates": 0, "recommendations": 0, "elapsed": time.monotonic() - t0}

    # Embedding 排序 + 候选生成（与 run_daily 一致：api reranker，bge-m3）
    generator = CandidateGenerator(config)
    pool = generator.generate(papers, corpus, date_str)
    candidates = pool.papers
    print(f"[backfill] {date_str}: 候选 {len(candidates)} 篇")

    # LLM 评测（OFM 网关 combo:deepread，bounded 重试，GPU 503 等待）
    llm_cfg = config.llm
    profile = str(config.get("research", {}).get("profile", ""))
    batch_size = int(config.get("research", {}).get("assessment_batch_size", 8))
    assessed = None
    for attempt in range(1, 4):
        try:
            assessed = assessment.assess_candidates(
                executor.openai_client,
                llm_cfg,
                candidates,
                profile,
                recent_papers=recent_papers,
                batch_size=batch_size,
            )
            break
        except Exception as exc:
            print(f"[backfill] {date_str}: LLM 评测第 {attempt} 次失败：{exc}")
            if attempt < 3:
                time.sleep(30.0)
            else:
                raise

    recommendations = assessment.build_slate(assessed, config.get("research", {}))
    print(f"[backfill] {date_str}: 推荐 {len(recommendations)} 篇")

    record = build_daily_record(
        report_date=date_str,
        retrieved_count=retrieved_count,
        candidates=assessed,
        recommendations=recommendations,
        status="complete",
    )
    # 回填不发邮件，标记为跳过发信，但本次运行算成功
    record["delivery_status"] = "skipped_no_email"

    if not dry_run:
        write_daily_record(record, DAILY_DIR)
        print(f"[backfill] {date_str}: 已写入 data/daily/{date_str}.json")

    elapsed = time.monotonic() - t0
    return {
        "date": date_str, "status": "ok", "retrieved": retrieved_count,
        "candidates": len(assessed), "recommendations": len(recommendations),
        "elapsed": elapsed,
    }


# --------------------------------------------------------------------------- #
# 深读 + 生成站点
# --------------------------------------------------------------------------- #
def deep_read_file(json_path: Path) -> int:
    """用 tools/deep_read.py 深读（--workers 1）。"""
    venv_python = REPO / ".venv" / "bin" / "python"
    deep_read = REPO / "tools" / "deep_read.py"
    cmd = [str(venv_python), str(deep_read), str(json_path), "--workers", "1"]
    print(f"[backfill] deep_read: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=2 * 3600)
    if res.stdout:
        print(res.stdout[-4000:])
    if res.returncode != 0:
        print(f"[backfill] deep_read 失败（rc={res.returncode}）：")
        print(res.stderr[-4000:] if res.stderr else "(no stderr)")
    return res.returncode


def generate_site() -> int:
    venv_python = REPO / ".venv" / "bin" / "python"
    site = REPO / "tools" / "generate_site.py"
    cmd = [str(venv_python), str(site)]
    print(f"[backfill] generate_site: {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=3600)
    if res.stdout:
        print(res.stdout[-2000:])
    if res.returncode != 0:
        print(f"[backfill] generate_site 失败（rc={res.returncode}）：")
        print(res.stderr[-2000:] if res.stderr else "(no stderr)")
    return res.returncode


def curl_check(date_str: str) -> bool:
    """curl 站点首页与索引，确认日期出现。"""
    for url in ("http://127.0.0.1:18970/papers/", "http://127.0.0.1:18970/papers/data/daily-index.json"):
        try:
            resp = requests.get(url, timeout=20)
            body = resp.text
            ok = date_str in body
            print(f"[backfill] curl {url} HTTP {resp.status_code} 日期{'出现' if ok else '未出现'}")
            if ok:
                return True
        except requests.RequestException as exc:
            print(f"[backfill] curl {url} 异常：{exc}")
    return False


# --------------------------------------------------------------------------- #
# 日期集合
# --------------------------------------------------------------------------- #
def weekdays_between(from_date: dt.date, to_date: dt.date) -> list[str]:
    out = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:  # 周一到周五
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def missing_dates(from_date: dt.date, to_date: dt.date) -> list[str]:
    return [d for d in weekdays_between(from_date, to_date)
            if not (DAILY_DIR / f"{d}.json").exists()]


def parse_dates(argv: list[str]) -> list[str]:
    dates: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--range" and i + 2 < len(argv):
            d0 = dt.date.fromisoformat(argv[i + 1])
            d1 = dt.date.fromisoformat(argv[i + 2])
            dates.extend(weekdays_between(d0, d1))
            i += 3
        elif a == "--all-missing":
            dates.extend(missing_dates(BACKFILL_FROM, BACKFILL_TO))
            i += 1
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", a or ""):
            dates.append(a)
            i += 1
        else:
            raise SystemExit(f"未知参数：{a}")
    # 去重、排序、只保留目标窗口内的平日
    allowed = set(weekdays_between(BACKFILL_FROM, BACKFILL_TO))
    dates = sorted({d for d in dates if d in allowed})
    return dates


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("用法：python tools/backfill.py 2026-10-23 [2026-10-26 ...]")
        print("      python tools/backfill.py --range 2026-10-23 2026-11-16")
        print("      python tools/backfill.py --all-missing")
        return 1

    dates = parse_dates(argv)
    if not dates:
        print("没有需要回填的日期")
        return 0

    config = _load_config()

    # Zotero 库（与 run_daily 一致）
    executor = Executor(config)
    corpus = executor.fetch_zotero_corpus()
    corpus = executor.filter_corpus(corpus)
    recent_count = int(config.get("research", {}).get("recent_zotero_count", 40))
    recent_papers = sorted(corpus, key=lambda c: c.added_date, reverse=True)[:recent_count]
    print(f"[backfill] Zotero corpus：{len(corpus)} 篇，最近 {len(recent_papers)} 篇用于评测上下文")

    start_time = time.monotonic()
    results: list[dict[str, Any]] = []

    for idx, date_str in enumerate(dates, start=1):
        elapsed_total = time.monotonic() - start_time
        remaining = TOTAL_TIME_LIMIT - elapsed_total
        print(f"\n[backfill] 进度 {idx}/{len(dates)}：{date_str}（剩余时限约 {int(remaining)//60} 分钟）")
        if remaining <= 60:
            print("[backfill] 剩余时限不足，停止回填")
            break

        try:
            r = backfill_one_date(date_str, config, executor, corpus, recent_papers)
            results.append(r)
        except Exception as exc:
            print(f"[backfill] {date_str}: 失败：{exc}")
            results.append({"date": date_str, "status": "failed", "error": str(exc),
                            "elapsed": time.monotonic() - start_time})
            continue

        json_path = DAILY_DIR / f"{date_str}.json"
        if r["status"] in ("ok", "empty") and json_path.exists():
            # 深读（--workers 1）
            rc = deep_read_file(json_path)
            r["deep_read_rc"] = rc
            # 生成站点
            rc2 = generate_site()
            r["generate_site_rc"] = rc2
            # 验证日期出现在站点
            curl_check(date_str)

    # ------------------------------------------------------------------ #
    # 汇报
    # ------------------------------------------------------------------ #
    print("\n============================================================")
    print("回填汇报：")
    ok_days = [r for r in results if r["status"] in ("ok", "empty")]
    fail_days = [r for r in results if r["status"] == "failed"]
    for r in results:
        print(
            f"  {r['date']}: {r['status']} | 抓取 {r.get('retrieved', '-')} | "
            f"候选 {r.get('candidates', '-')} | 推荐 {r.get('recommendations', '-')} | "
            f"耗时 {r.get('elapsed', 0):.0f}s"
        )
    print(f"成功：{len(ok_days)} 天；失败：{len(fail_days)} 天")
    for r in fail_days:
        print(f"  失败 {r['date']}: {r.get('error')}")
    total_elapsed = time.monotonic() - start_time
    print(f"总耗时：{total_elapsed:.0f}s ({total_elapsed/60:.1f} 分钟)")
    print("============================================================")
    return 1 if fail_days else 0


if __name__ == "__main__":
    raise SystemExit(main())
