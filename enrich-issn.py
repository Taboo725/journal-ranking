"""
enrich-issn.py — 自动为 journals.json 中缺少 ISSN 的期刊补全 ISSN 号

策略：
  - 英文刊名：CrossRef journals API → 失败则 OpenAlex
  - 中文刊名：直接 OpenAlex（CrossRef 覆盖极差）
  - 精确匹配（normalize 后完全相同）直接采用
  - 模糊匹配（相似度 ≥ FUZZY_THRESHOLD）也自动采用
  - API 响应结果缓存到 .enrich-cache.json，中断后可续跑

用法：
  python enrich-issn.py              # 正常运行
  python enrich-issn.py --dry-run    # 只统计，不写入
  python enrich-issn.py --limit 100  # 只处理前 N 条（测试用）
  python enrich-issn.py --clear-cache
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
from difflib import SequenceMatcher
from pathlib import Path

# ── 路径 ────────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).resolve().parent
JOURNALS_PATH   = SCRIPT_DIR / "journals.json"
OVERRIDES_PATH  = SCRIPT_DIR / "manual-overrides.json"
CACHE_PATH      = SCRIPT_DIR / ".enrich-cache.json"

# ── 参数 ────────────────────────────────────────────────────────────────────
MAILTO          = "scholarx-bot@example.com"   # CrossRef polite pool
FUZZY_THRESHOLD = 0.92                          # 模糊匹配阈值
CROSSREF_DELAY  = 0.06                          # s / request
OPENALEX_DELAY  = 0.12                          # s / request（10 req/s 限速）
SAVE_INTERVAL   = 100                           # 每 N 条成功写一次 overrides


# ── 工具函数 ─────────────────────────────────────────────────────────────────
def normalize_name(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"&amp;(amp;)?|&", "and", text, flags=re.IGNORECASE)
    text = text.replace("英文版", "英文").replace("学版", "学")
    text = re.sub(r"^the\s+", "", text, flags=re.IGNORECASE)
    return re.sub(r"[\s\u00a0\-_–—.,、:;()（）·•*，：；+®/\"'<>《》]+", "", text)


def fmt_issn(raw: object) -> str | None:
    if raw is None:
        return None
    s = re.sub(r"[^0-9Xx]", "", str(raw)).upper()
    if len(s) == 8:
        return f"{s[:4]}-{s[4:]}"
    return None


def is_chinese(name: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", name))


def fetch_json(url: str, timeout: int = 15) -> dict | list | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"ScholarX/1.0 (mailto:{MAILTO})"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


# ── API 查询 ─────────────────────────────────────────────────────────────────
def query_crossref(name: str) -> list[dict]:
    url = (
        f"https://api.crossref.org/journals"
        f"?query={urllib.parse.quote(name)}&rows=3&mailto={MAILTO}"
    )
    data = fetch_json(url)
    time.sleep(CROSSREF_DELAY)
    if not data:
        return []
    results = []
    for item in data.get("message", {}).get("items", []):
        title = item.get("title", "")
        issn_types = {t["type"]: t["value"] for t in item.get("issn-type", [])}
        issns = item.get("ISSN", [])
        issn  = fmt_issn(issn_types.get("print")       or (issns[0] if issns else None))
        eissn = fmt_issn(issn_types.get("electronic")  or (issns[1] if len(issns) > 1 else None))
        if title:
            results.append({"name": title, "issn": issn, "eissn": eissn})
    return results


def query_openalex(name: str) -> list[dict]:
    url = (
        f"https://api.openalex.org/sources"
        f"?search={urllib.parse.quote(name)}&filter=type:journal&per-page=3"
    )
    data = fetch_json(url)
    time.sleep(OPENALEX_DELAY)
    if not data:
        return []
    results = []
    for item in data.get("results", []):
        title = item.get("display_name", "")
        issns = [fmt_issn(x) for x in (item.get("issn") or []) if fmt_issn(x)]
        issn_l = fmt_issn(item.get("issn_l"))
        issn  = issns[0] if issns else issn_l
        eissn = issns[1] if len(issns) > 1 else None
        if title:
            results.append({"name": title, "issn": issn, "eissn": eissn})
    return results


# ── 匹配 ─────────────────────────────────────────────────────────────────────
def best_match(name: str, candidates: list[dict]) -> tuple[dict | None, float]:
    key = normalize_name(name)
    best_r = 0.0
    best_c: dict | None = None
    for c in candidates:
        ckey = normalize_name(c.get("name", ""))
        if ckey == key:
            return c, 1.0
        r = SequenceMatcher(None, key, ckey).ratio()
        if r > best_r:
            best_r, best_c = r, c
    if best_r >= FUZZY_THRESHOLD:
        return best_c, best_r
    return None, best_r


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",     action="store_true", help="只统计，不写入")
    parser.add_argument("--clear-cache", action="store_true", help="清除缓存后重新运行")
    parser.add_argument("--limit",       type=int, default=0, help="限制处理条数（测试用）")
    args = parser.parse_args()

    if args.clear_cache and CACHE_PATH.exists():
        CACHE_PATH.unlink()
        print("缓存已清除")

    # ── 加载数据 ──────────────────────────────────────────────────────────────
    journals  = json.loads(JOURNALS_PATH.read_text(encoding="utf-8"))
    cache     = json.loads(CACHE_PATH.read_text(encoding="utf-8")) if CACHE_PATH.exists() else {}
    overrides = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8")) if OVERRIDES_PATH.exists() else []

    # 已有 override 的刊名 key（避免重复）
    override_keys = {normalize_name(o.get("name", "")) for o in overrides if o.get("name")}

    missing = [j for j in journals if not j.get("issn") and not j.get("eissn") and j.get("name")]
    if args.limit:
        missing = missing[: args.limit]

    total   = len(missing)
    found   = 0
    skipped = 0
    failed  = 0
    new_entries: list[dict] = []

    print(f"缺 ISSN 期刊: {total} 条  |  dry-run={'是' if args.dry_run else '否'}")
    print("─" * 60)

    for idx, j in enumerate(missing, 1):
        name = j["name"].strip()
        key  = normalize_name(name)

        if key in override_keys:
            skipped += 1
            continue

        label = f"[{idx}/{total}] {name[:44]}"

        # ── 缓存命中 ──────────────────────────────────────────────────────────
        if key in cache:
            entry = cache[key]
            result = entry.get("result")
            ratio  = entry.get("ratio", 0.0)
        else:
            # ── 实时查询 ──────────────────────────────────────────────────────
            cn = is_chinese(name)
            result, ratio, source = None, 0.0, ""

            if not cn:
                cands = query_crossref(name)
                result, ratio = best_match(name, cands)
                source = "crossref"

            if result is None:
                cands = query_openalex(name)
                result, ratio = best_match(name, cands)
                source = "openalex"

            cache[key] = {"result": result, "ratio": ratio, "source": source}
            CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

        # ── 记录结果 ──────────────────────────────────────────────────────────
        if result and result.get("issn"):
            tag = f"✓ {result['issn']}"
            if result.get("eissn"):
                tag += f" / {result['eissn']}"
            tag += f"  ({ratio:.2f})"
            print(f"{label:<50} {tag}")
            found += 1
            entry_out: dict = {"name": name}
            if result.get("issn"):
                entry_out["issn"]  = result["issn"]
            if result.get("eissn"):
                entry_out["eissn"] = result["eissn"]
            new_entries.append(entry_out)
            override_keys.add(key)

            # 定期保存
            if not args.dry_run and len(new_entries) % SAVE_INTERVAL == 0:
                overrides.extend(new_entries[-SAVE_INTERVAL:])
                _save_overrides(overrides)
                print(f"  → 已中途保存 {len(overrides)} 条 overrides")
        else:
            print(f"{label:<50} ✗ ({ratio:.2f})")
            failed += 1

    # ── 最终写入 ──────────────────────────────────────────────────────────────
    print("─" * 60)
    print(f"找到: {found}  |  跳过(已有override): {skipped}  |  未匹配: {failed}")

    if not args.dry_run and new_entries:
        # 追加未保存的尾部
        already_saved = (found // SAVE_INTERVAL) * SAVE_INTERVAL
        tail = new_entries[already_saved:]
        overrides.extend(tail)
        _save_overrides(overrides)
        print(f"✓ manual-overrides.json 已更新（共 {len(overrides)} 条）")

        print("\n正在重新构建期刊数据库 ...")
        subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "build-data.py"), "--sync"],
            check=True,
        )


def _save_overrides(overrides: list[dict]) -> None:
    OVERRIDES_PATH.write_text(
        json.dumps(overrides, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
