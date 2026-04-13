"""
ScholarX 期刊数据构建脚本
用法：
  python build-data.py
  python build-data.py --sync

输入：
  manual-overrides.json（手工覆盖项，可直接编辑）
  journals.json（由 --sync 自动生成的全量期刊目录）
输出：odata.json（上传到 gitee/github 作为数据源）

字段说明（journals.json / manual-overrides.json 每条记录）：
  name   : 期刊全名（支持中文）
  issn   : 印刷版 ISSN，格式 XXXX-XXXX
  eissn  : 电子版 ISSN，格式 XXXX-XXXX（可为空）
  abbr   : 常用缩写名
  IF     : 影响因子（数字，无则 null）
  bank   : WoS 收录类型，可选值：
             "SCIE" | "SSCI" | "ESCI" | "AHCI" |
             "SCIE/SSCI" | "SCIE/SSCI/AHCI" | "SCIE/AHCI" | "SSCI/AHCI"
             （无收录则 null）
  jcr    : JCR 分区，1~4（无则 null）
  cas    : 中科院分区，1~4（无则 null）；Top 期刊且 top=true 时内部存为 cas+10
  top    : 是否中科院 Top 期刊，true/false
  ei     : 是否 EI 收录，true/false
  cscd   : CSCD 收录，1=核心库 2=扩展库（无则 null）
  pku    : 是否北大核心，true/false
  sos    : 预警期刊，格式 {"24": 1} 表示 2024年高预警
             预警等级：1=高预警 2=中预警 3=低预警 4=引用操纵
             5=引用操纵/论文工厂 6=论文工厂 7=论文工厂/CN占比畸高 8=CN占比畸高
             （无预警则 null）
  utd24  : 是否 UTD24 核心期刊，true/false
  ft50   : 是否 FT50 期刊，true/false
  abs    : ABS/AJG 等级，1 / 2 / 3 / 4 / "4*"（无则 null）
  cssci  : CSSCI 收录，1=核心期刊 2=扩展版（无则 null）
  njubs_cn: 南京大学商学院中文目录，1=一流 2=权威 3=核心（无则 null）
  njubs_en: 南京大学商学院英文目录，1~4=1区~4区（无则 null）
  cnki_if: CNKI 综合影响因子（仅期刊引用，数字，无则 null）
  cnki_ifs: CNKI 复合影响因子（含学位论文等，来源更广，数字，无则 null）
"""

import argparse
import base64
import json
import os
import re

BANK_MAP = {
    "SCIE": 1, "SSCI": 2, "ESCI": 3, "AHCI": 4,
    "SCIE/SSCI": 5, "SCIE/SSCI/AHCI": 6, "SCIE/AHCI": 7, "SSCI/AHCI": 8,
}

PREFIX = "SCHX_"  # 5字符前缀（loader 会 slice(5) 跳过）

# 与 src/data/journal-loader.ts 中的 DATA_VERSION 保持一致；
# 修改此值时须同步修改 journal-loader.ts 中的 DATA_VERSION，以触发扩展重新拉取远端数据。
BUILD_VERSION = "jt_260413"


def encode(obj) -> str:
    """将 Python 对象序列化为 JSON，Base64 编码，加前缀"""
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return PREFIX + b64


def normalize_name(name: str) -> str:
    """标准化期刊名为 jdata/jabb 的 key，与 journal-loader.ts 中 normalizeName() 保持一致"""
    name = name.strip().lower()
    name = re.sub(r"&amp;(amp;)?|&", "and", name, flags=re.IGNORECASE)
    # 移除前缀 "the "、特定词、所有空白和标点
    name = re.sub(
        r"^the |switzerland|basel|\s|\n|\u00a0|[-–—.,、→_:;()·•*，：；+（）®《》<>\"\"\"'/?]+",
        "", name, flags=re.IGNORECASE
    )
    name = name.replace("英文版", "英文").replace("学版", "学")
    return name


def jdata_key(name: str) -> str:
    """jdata 表的 key = normalize 后再将 journal→xx, international→ii"""
    key = normalize_name(name)
    key = re.sub(r"journal", "xx", key, flags=re.IGNORECASE)
    key = re.sub(r"international", "ii", key, flags=re.IGNORECASE)
    return key


def issn_key(issn: str) -> str:
    """ISSN key = 小写去横线"""
    return issn.lower().replace("-", "").strip()


def build_info(j: dict) -> dict:
    """将 journals.json 中一条记录转换为内部 JournalInfo 对象"""
    info = {}
    if j.get("IF") is not None:
        info["A"] = round(float(j["IF"]), 3)
    bank = BANK_MAP.get(j.get("bank") or "")
    if bank:
        info["CA"] = bank
    if j.get("jcr") is not None:
        info["C"] = int(j["jcr"])
    if j.get("cas") is not None:
        cas = int(j["cas"])
        info["D"] = cas + 10 if j.get("top") else cas
    elif j.get("top"):
        # 有 top 但没写 cas，补 11（1区Top）
        info["D"] = 11
    if j.get("top"):
        info["E"] = 1
    if j.get("ei"):
        info["F"] = 1
    if j.get("cscd") is not None:
        info["G"] = int(j["cscd"])
    if j.get("pku"):
        info["H"] = 1
    if j.get("sos"):
        info["S"] = j["sos"]
    # 经管专业分级
    if j.get("utd24"):
        info["I"] = 1
    if j.get("ft50"):
        info["J"] = 1
    abs_val = j.get("abs")
    if abs_val is not None:
        info["K"] = 5 if str(abs_val) == "4*" else int(abs_val)
    if j.get("cssci") is not None:
        info["L"] = int(j["cssci"])
    if j.get("njubs_cn") is not None:
        info["O"] = int(j["njubs_cn"])
    if j.get("njubs_en") is not None:
        info["P"] = int(j["njubs_en"])
    if j.get("cnki_if") is not None:
        info["M"] = round(float(j["cnki_if"]), 3)
    if j.get("cnki_ifs") is not None:
        info["N"] = round(float(j["cnki_ifs"]), 3)
    if j.get("swufe") is not None:
        info["Q"] = int(j["swufe"])
    if j.get("sufe_soe") is not None:
        info["R"] = int(j["sufe_soe"])
    if j.get("fdu_som") is not None:
        info["T"] = int(j["fdu_som"])
    if j.get("njubs_sa") is not None:
        info["U"] = int(j["njubs_sa"])
    return info


def main():
    parser = argparse.ArgumentParser(description="构建 ScholarX 期刊数据")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="先从 ../journal/ 下的原始索引表回填 journals.json，再生成 odata.json",
    )
    args = parser.parse_args()

    if args.sync:
        from sync_indexes import sync_journals

        sync_journals()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_file  = os.path.join(script_dir, "journals.json")
    output_file = os.path.join(script_dir, "odata.json")

    with open(input_file, encoding="utf-8") as f:
        raw = f.read()
    # journals.json 支持 // 行注释（标准 JSON 不支持，此处预处理）
    raw = re.sub(r"^\s*//.*$", "", raw, flags=re.MULTILINE)
    journals = json.loads(raw)

    jdata: dict = {}   # 期刊名 key → ISSN key（或直接 info）
    jssn:  dict = {}   # ISSN key → JournalInfo
    jabb:  dict = {}   # 缩写/ISSN → ISSN key（或直接 info）
    jdisp: dict = {}   # 主 ISSN key → 期刊展示名（用于 source: 检索式构建）

    skipped = 0

    for j in journals:
        info = build_info(j)
        if not info:
            skipped += 1
            continue

        name  = j.get("name", "")
        issn  = j.get("issn", "")
        eissn = j.get("eissn", "")
        abbr  = j.get("abbr", "")

        issn_k  = issn_key(issn)  if issn  else None
        eissn_k = issn_key(eissn) if eissn else None
        name_k  = jdata_key(name)
        abbr_k  = normalize_name(abbr) if abbr else None

        # jssn：两个 ISSN 都指向 info
        if issn_k:
            jssn[issn_k] = info
        if eissn_k and eissn_k != issn_k:
            jssn[eissn_k] = info

        # jdata：期刊名 key → 主 ISSN（或直接 info）
        target = issn_k if issn_k else info
        if name_k:
            jdata[name_k] = target

        # jabb：缩写名 → 主 ISSN（或直接 info）
        if abbr_k and abbr_k != name_k:
            jabb[abbr_k] = target

        # jdisp：ISSN → 期刊展示名（用于 source: 检索式构建）
        # 优先用 print ISSN 作为主键（与 jssn 迭代时取到的主条目一致）；
        # 若仅有 eISSN（纯电子刊），则用 eISSN 作为主键。
        # 每个期刊只写一次，避免 GET_JOURNALS_BY_INDEX 重复计数。
        if name:
            if issn_k:
                jdisp[issn_k] = name
            elif eissn_k:
                jdisp[eissn_k] = name

    output = {
        "version": BUILD_VERSION,
        "jdata": encode(jdata),
        "jssn":  encode(jssn),
        "jabb":  encode(jabb),
        "jdisp": encode(jdisp),
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    # 同时输出解码版本，供扩展打包时内置（src/data/journal-db.json）
    bundle_file = os.path.join(script_dir, "..", "src", "data", "journal-db.json")
    bundle_file = os.path.normpath(bundle_file)
    with open(bundle_file, "w", encoding="utf-8") as f:
        json.dump({"jdata": jdata, "jssn": jssn, "jabb": jabb, "jdisp": jdisp}, f,
                  ensure_ascii=False, separators=(",", ":"))

    print(f"\n✓ 已生成 {output_file}")
    print(f"✓ 已生成 {bundle_file}")
    print(f"  输入期刊: {len(journals)}")
    print(f"  跳过空记录: {skipped}")
    print(f"  jdata 条目: {len(jdata)}")
    print(f"  jssn  条目: {len(jssn)}")
    print(f"  jabb  条目: {len(jabb)}")
    print("\n将 odata.json 上传到 gitee/github，然后更新 src/data/journal-loader.ts 中的 DATA_SOURCES。")


if __name__ == "__main__":
    main()
