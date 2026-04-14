"""
enrich-abbr.py — 为 journals.json 中缺少缩写名(abbr)的期刊自动补全

策略（批量模式）：
  - 将期刊按 BATCH_SIZE 分组
  - 每批一次 NLM esearch（OR 拼接所有 ISSN）+ 一次 esummary（WebEnv）
  - 通过 esummary 返回的 issnlist 字段将 UID 映射回原 ISSN
  - issnlist 为空的记录退回单条 CrossRef / OpenAlex 查询
  - 全程缓存到 .enrich-abbr-cache.json，中断后可续跑

速度对比（无 API Key，3 req/s）：
  旧逐条模式: ~0.72 s/条 → 22000条 ≈ 4.5 小时
  批量模式:   ~0.07 s/条 → 22000条 ≈ 26 分钟

用法：
  python enrich-abbr.py                  # 正常运行
  python enrich-abbr.py --dry-run        # 只统计，不写入
  python enrich-abbr.py --limit 200      # 仅处理前 N 条
  python enrich-abbr.py --clear-cache    # 清缓存重跑
  python enrich-abbr.py --api-key KEY    # NCBI API Key（免费注册可获，提速到 ~8 分钟）
  python enrich-abbr.py --with-fallback  # 对 NLM 未命中的条目额外查 CrossRef/OpenAlex（更慢）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────────────
SCRIPT_DIR     = Path(__file__).resolve().parent
JOURNALS_PATH  = SCRIPT_DIR / "journals.json"
ABBR_DATA_PATH = SCRIPT_DIR / "abbr-data.json"
CACHE_PATH     = SCRIPT_DIR / ".enrich-abbr-cache.json"

# ── 参数 ────────────────────────────────────────────────────────────────────
MAILTO        = "scholarx-bot@example.com"
BATCH_SIZE    = 50      # NLM 批量查询每批 ISSN 数
NLM_DELAY     = 0.36    # s/req 无 Key（3 req/s 上限）
NLM_DELAY_KEY = 0.11    # s/req 有 Key（10 req/s 上限）
CR_DELAY      = 0.06    # CrossRef 兜底延迟
OA_DELAY      = 0.12    # OpenAlex 兜底延迟
SAVE_INTERVAL = 500     # 每 N 条成功中途保存


# ── 工具 ─────────────────────────────────────────────────────────────────────
def normalize_issn(raw: object) -> str | None:
    if raw is None:
        return None
    s = re.sub(r"[^0-9Xx]", "", str(raw)).upper()
    return f"{s[:4]}-{s[4:]}" if len(s) == 8 else None


def normalize_name(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"&amp;(amp;)?|&", "and", text, flags=re.IGNORECASE)
    return re.sub(r"[\s\u00a0\-_–—.,、:;()（）·•*，：；+®/\"'<>《》]+", "", text)


def fetch_json(url: str, timeout: int = 20) -> dict | list | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"ScholarX/1.0 (mailto:{MAILTO})"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def is_trivial_abbr(abbr: str, full_name: str) -> bool:
    return normalize_name(abbr) == normalize_name(full_name)


# ── NLM 批量查询 ──────────────────────────────────────────────────────────────
def nlm_batch(issn_list: list[str], api_key: str, delay: float) -> dict[str, str]:
    """
    批量查询 NLM，返回 {issn_上层输入: medlineta} 字典。
    issn_list: 本批次所有 ISSN（已规范化）
    """
    # Step 1: esearch（OR 拼接）
    term = " OR ".join(f"{i}[issn]" for i in issn_list)
    params: dict = {
        "db": "nlmcatalog",
        "term": term,
        "retmode": "json",
        "retmax": str(len(issn_list) * 2),  # 同 ISSN 可能有多条记录
        "usehistory": "y",
    }
    if api_key:
        params["api_key"] = api_key
    url1 = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(params)
    d1 = fetch_json(url1)
    time.sleep(delay)
    if not d1:
        return {}

    result1 = d1.get("esearchresult", {})
    ids     = result1.get("idlist", [])
    web_env = result1.get("webenv", "")
    qkey    = result1.get("querykey", "")
    if not ids or not web_env:
        return {}

    # Step 2: esummary（按 WebEnv 批取）
    params2: dict = {
        "db": "nlmcatalog",
        "query_key": qkey,
        "WebEnv": web_env,
        "retmode": "json",
        "retmax": str(len(ids)),
    }
    if api_key:
        params2["api_key"] = api_key
    url2 = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(params2)
    d2 = fetch_json(url2)
    time.sleep(delay)
    if not d2:
        return {}

    # 构建 ISSN→abbr 映射（从 issnlist 字段反查）
    result_map: dict[str, str] = {}
    query_set = {i.upper().replace("-", "") for i in issn_list}

    for uid, item in d2.get("result", {}).items():
        if uid == "uids":
            continue
        abbr = (item.get("medlineta") or "").strip()
        if not abbr:
            continue

        issn_entries = item.get("issnlist") or []
        for entry in issn_entries:
            raw_issn = (entry.get("issn") if isinstance(entry, dict) else str(entry)).strip()
            canonical = normalize_issn(raw_issn)
            if canonical:
                raw_norm = canonical.upper().replace("-", "")
                if raw_norm in query_set:
                    # 只在请求列表里的 ISSN 才存入
                    result_map[canonical] = abbr

    return result_map


# ── 单条兜底 ───────────────────────────────────────────────────────────────────
def crossref_abbr(issn: str) -> str | None:
    url = f"https://api.crossref.org/journals/{urllib.parse.quote(issn)}?mailto={MAILTO}"
    data = fetch_json(url)
    time.sleep(CR_DELAY)
    if not data:
        return None
    titles = data.get("message", {}).get("short-container-title") or []
    return (titles[0].strip() if isinstance(titles, list) and titles else None) or None


def openalex_abbr(issn: str) -> str | None:
    url = f"https://api.openalex.org/sources/issn:{urllib.parse.quote(issn)}"
    data = fetch_json(url)
    time.sleep(OA_DELAY)
    if not data:
        return None
    return (data.get("abbreviated_title") or "").strip() or None


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--clear-cache", action="store_true")
    parser.add_argument("--limit",       type=int, default=0)
    parser.add_argument("--api-key",     type=str, default="")
    parser.add_argument("--with-fallback", action="store_true", help="NLM 未命中时额外查 CrossRef/OpenAlex（慢）")
    args = parser.parse_args()

    api_key      = args.api_key.strip()
    delay        = NLM_DELAY_KEY if api_key else NLM_DELAY
    with_fallback = args.with_fallback

    if args.clear_cache and CACHE_PATH.exists():
        CACHE_PATH.unlink()
        print("缓存已清除")

    # ── 加载 ──────────────────────────────────────────────────────────────────
    journals  = json.loads(JOURNALS_PATH.read_text(encoding="utf-8"))
    cache: dict[str, str | None] = (
        json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    )
    abbr_entries: list[dict] = (
        json.loads(ABBR_DATA_PATH.read_text(encoding="utf-8")) if ABBR_DATA_PATH.exists() else []
    )

    # abbr-data 索引（用于更新已有条目而非重复追加）
    issn_to_idx: dict[str, int] = {}
    name_to_idx: dict[str, int] = {}
    for i, o in enumerate(abbr_entries):
        for f in ("issn", "eissn"):
            v = normalize_issn(o.get(f))
            if v:
                issn_to_idx[v] = i
        nk = normalize_name(o.get("name", ""))
        if nk:
            name_to_idx[nk] = i

    # 已有缩写的 ISSN 集合（journals.json 已包含 abbr-data 应用后的结果）
    existing_abbr: set[str] = set()
    for j in journals:
        if j.get("abbr"):
            for f in ("issn", "eissn"):
                v = normalize_issn(j.get(f))
                if v:
                    existing_abbr.add(v)
    # 还需检查 abbr-data.json 中已有但尚未 sync 的条目
    for o in abbr_entries:
        if o.get("abbr"):
            for f in ("issn", "eissn"):
                v = normalize_issn(o.get(f))
                if v:
                    existing_abbr.add(v)

    # 候选列表（有 ISSN、无缩写、未在缓存中命中）
    candidates: list[tuple[str, str | None, str | None]] = []  # (name, issn, eissn)
    for j in journals:
        issn  = normalize_issn(j.get("issn"))
        eissn = normalize_issn(j.get("eissn"))
        if not issn and not eissn:
            continue
        if j.get("abbr"):
            continue
        primary = issn or eissn
        if primary in existing_abbr:
            continue
        if primary in cache:
            continue   # 已缓存（无论是否命中）
        candidates.append((j["name"], issn, eissn))

    if args.limit:
        candidates = candidates[: args.limit]

    # 统计已缓存命中数（本次跳过但已有结果）
    cached_found = sum(1 for v in cache.values() if v)

    total  = len(candidates)
    found  = 0
    failed = 0
    new_entries: list[tuple[int | None, dict]] = []

    est_minutes = total * delay * 2 / BATCH_SIZE / 60 + (total * 0.18 / 60 if with_fallback else 0)
    print(f"待处理: {total} 条（另有 {cached_found} 条来自缓存）")
    print(f"批大小: {BATCH_SIZE}  |  速率: {'有Key' if api_key else '无Key'}  |  dry-run={'是' if args.dry_run else '否'}")
    print(f"预计耗时: ~{est_minutes:.0f} 分钟")
    print("─" * 60)

    # ── 批量处理 ──────────────────────────────────────────────────────────────
    for batch_start in range(0, total, BATCH_SIZE):
        batch = candidates[batch_start: batch_start + BATCH_SIZE]

        # 收集本批次中需要查询的 ISSN（优先用主 ISSN）
        issn_map: dict[str, tuple[str, str | None, str | None]] = {}  # issn → (name,issn,eissn)
        for name, issn, eissn in batch:
            primary = issn or eissn
            if primary and primary not in issn_map:
                issn_map[primary] = (name, issn, eissn)
            if eissn and eissn != issn and eissn not in issn_map:
                issn_map[eissn] = (name, issn, eissn)

        # NLM 批量
        nlm_results = nlm_batch(list(issn_map.keys()), api_key, delay)

        # 把结果应用到每条期刊
        for name, issn, eissn in batch:
            primary  = issn or eissn
            fallback = eissn if (issn and eissn and issn != eissn) else None

            abbr = nlm_results.get(primary) or (nlm_results.get(fallback) if fallback else None)

            # 缺失：单条兜底（可选，默认跳过以保持速度）
            if not abbr and with_fallback:
                abbr = crossref_abbr(primary)
                if not abbr and fallback:
                    abbr = crossref_abbr(fallback)
                if not abbr:
                    abbr = openalex_abbr(primary)
                    if not abbr and fallback:
                        abbr = openalex_abbr(fallback)

            # 缩写等于全名无意义
            if abbr and is_trivial_abbr(abbr, name):
                abbr = None

            cache[primary] = abbr
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

            if abbr:
                found += 1
                ov_idx = (issn_to_idx.get(primary)
                          or (issn_to_idx.get(fallback) if fallback else None)
                          or name_to_idx.get(normalize_name(name)))
                new_entries.append((ov_idx, {"name": name, "issn": issn, "eissn": eissn, "abbr": abbr}))
            else:
                failed += 1

        # 进度报告（每批）
        done = batch_start + len(batch)
        print(f"[{done}/{total}] 批次完成  找到:{found}  未找到:{failed}", flush=True)

        # 中途保存
        if not args.dry_run and len(new_entries) >= SAVE_INTERVAL:
            _apply_and_save(abbr_entries, new_entries[:SAVE_INTERVAL], issn_to_idx, name_to_idx)
            new_entries = new_entries[SAVE_INTERVAL:]
            print(f"  → 中途保存，abbr-data 共 {len(abbr_entries)} 条", flush=True)

    # ── 最终写入 ──────────────────────────────────────────────────────────────
    print("─" * 60)
    print(f"完成  找到: {found}  |  未找到: {failed}  |  总命中: {cached_found + found}")

    if not args.dry_run and (new_entries or found > 0):
        _apply_and_save(abbr_entries, new_entries, issn_to_idx, name_to_idx)
        _write_abbr_data(abbr_entries)
        print(f"✓ abbr-data.json 已更新（共 {len(abbr_entries)} 条）")

        print("\n正在重新构建期刊数据库 ...")
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "build-data.py"), "--sync"],
            check=True,
        )


def _apply_and_save(
    abbr_entries: list[dict],
    batch: list[tuple[int | None, dict]],
    issn_to_idx: dict[str, int],
    name_to_idx: dict[str, int],
) -> None:
    for ov_idx, entry in batch:
        if ov_idx is not None:
            existing = abbr_entries[ov_idx]
            for k, v in entry.items():
                if v and not existing.get(k):
                    existing[k] = v
        else:
            new_entry = {k: v for k, v in entry.items() if v}
            abbr_entries.append(new_entry)
            new_idx = len(abbr_entries) - 1
            for f in ("issn", "eissn"):
                v = normalize_issn(new_entry.get(f))
                if v:
                    issn_to_idx[v] = new_idx
            nk = normalize_name(new_entry.get("name", ""))
            if nk:
                name_to_idx[nk] = new_idx
    _write_abbr_data(abbr_entries)


def _write_abbr_data(abbr_entries: list[dict]) -> None:
    ABBR_DATA_PATH.write_text(
        json.dumps(abbr_entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
