#!/usr/bin/env bash
# run_daily.sh — zotero-arxiv-daily 每日运行入口（不发信版）
#
# 流程：
#   1. 跑完整流程（Zotero 库 + arXiv 新文 -> 候选 -> LLM 评测 -> 每日记录）
#   2. 更新 data/daily/YYYY-MM-DD.json（保留历史）
#   3. 重新生成静态页面到 /workspace/sites/papers/
#   4. 写入 data/latest_telegram.txt（Telegram 精简文字，最多 4 篇）
#
# 设计：
#   - 邮件不是硬依赖：executor.send_email=false，不发信也算成功；不使用任何真实邮箱。
#   - 密钥只从环境变量/凭据文件读取，不打印、不写入仓库、不提交。
#   - LLM 走本地网关，并发由 LLM 端点本身控制（评测按批串行）。
#
# 所需凭据（运行前已注入环境，或由本脚本自动读取）：
#   ZOTERO_ID / ZOTERO_KEY   -> /workspace/.secrets/zotero.env
#   OPENAI_API_KEY           -> /home/node/.dsh/.credentials.yaml 中的 OFM_API_KEY
#
# 用法：./run_daily.sh

set -euo pipefail

REPO="/workspace/zotero-arxiv-daily"
SECRETS_ENV="/workspace/.secrets/zotero.env"
CREDENTIALS="/home/node/.dsh/.credentials.yaml"
TELEGRAM_OUT="$REPO/data/latest_telegram.txt"

cd "$REPO"

# 载入 Zotero 只读凭据（不打印、不入仓库）
if [[ -f "$SECRETS_ENV" ]]; then
  set -a; . "$SECRETS_ENV"; set +a
fi

# LLM 密钥：从 credentials.yaml 的 OFM_API_KEY 读取，映射为 OpenAI 兼容客户端所需的 OPENAI_API_KEY
if [[ -z "${OPENAI_API_KEY:-}" && -f "$CREDENTIALS" ]]; then
  export OPENAI_API_KEY="$(python3 - "$CREDENTIALS" <<'PY'
import sys, re
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'^\s*OFM_API_KEY:\s*(\S+)$', text, flags=re.M)
if not m:
    raise SystemExit("OFM_API_KEY not found in credentials file")
print(m.group(1))
PY
)"
fi
export OPENAI_API_BASE="${OPENAI_API_BASE:-http://127.0.0.1:18900/v1}"
export OPENAI_API_MODEL="${OPENAI_API_MODEL:-deepseek-v4.1-flash-free}"

if [[ -z "${ZOTERO_ID:-}" || -z "${ZOTERO_KEY:-}" || -z "${OPENAI_API_KEY:-}" ]]; then
  echo "缺少凭据：请先设置 ZOTERO_ID/ZOTERO_KEY（$SECRETS_ENV）和 OPENAI_API_KEY（$CREDENTIALS 中的 OFM_API_KEY）" >&2
  exit 1
fi

echo "[1/4] 运行 zotero-arxiv-daily 全流程（不发信模式）…"
PYTHONPATH=src .venv/bin/python src/zotero_arxiv_daily/main.py \
  executor.send_email=false \
  executor.reranker=api \
  executor.retriever_sleep_seconds=0.2 \
  llm.api.base_url="$OPENAI_API_BASE" \
  llm.generation_kwargs.model="$OPENAI_API_MODEL"

LATEST_JSON="$(ls -1 data/daily/*.json | sort | tail -1)"
echo "最新每日记录：$LATEST_JSON"

echo "[2/4] 为最新每日记录补生成“深读”（背景/做法/结果/局限 + 先补一课）…"
python3 tools/deep_read.py "$LATEST_JSON" || true

echo "[3/4] 重新生成静态页面 -> /workspace/sites/papers/"
python3 tools/generate_site.py --source data/daily --output /workspace/sites/papers

echo "[4/4] 生成 Telegram 精简文字 -> $TELEGRAM_OUT"
python3 - "$LATEST_JSON" "$TELEGRAM_OUT" <<'PY'
import json, sys, pathlib
src, out = sys.argv[1], sys.argv[2]
record = json.loads(open(src, encoding="utf-8").read())
recs = record.get("recommendations", [])[:4]
lines = []
lines.append(f"arXiv 日报 · {record.get('date')}")
lines.append("")
for r in recs:
    title = (r.get("title") or "").strip()
    tldr = ((r.get("assessment") or {}).get("contribution") or {}).get("zh") or ""
    url = r.get("url") or ""
    lines.append(title)
    if tldr:
        lines.append("一句 TLDR：" + tldr.strip())
    if url:
        lines.append("链接：" + url)
    lines.append("")
pathlib.Path(out).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
print(f"写入 {out}，共 {len(recs)} 篇")
PY

echo "全部完成。静态页面入口：/workspace/sites/papers/index.html"
