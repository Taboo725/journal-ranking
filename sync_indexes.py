"""
ScholarX 全量期刊目录同步脚本

用途：
  1. 从 ../journal/ 中自动读取最新的 WoS/JCR/CAS 原始文件
  2. 生成完整的 journals.json
  3. 应用 manual-overrides.json 中的手工覆盖项

推荐工作流：
  python sync_indexes.py
  python build-data.py --sync

说明：
  - journals.json 是全量生成后的可视目录
  - manual-overrides.json 是小型手工维护文件，只放需要人工补充/修正的字段
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Callable, Iterable
from xml.etree import ElementTree as ET
from zipfile import ZipFile


XLSX_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "office": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "package": "http://schemas.openxmlformats.org/package/2006/relationships",
}

WOS_BANKS = ("SCIE", "SSCI", "ESCI", "AHCI")
SUPPORTED_BANKS = {
    frozenset({"SCIE"}): "SCIE",
    frozenset({"SSCI"}): "SSCI",
    frozenset({"ESCI"}): "ESCI",
    frozenset({"AHCI"}): "AHCI",
    frozenset({"SCIE", "SSCI"}): "SCIE/SSCI",
    frozenset({"SCIE", "SSCI", "AHCI"}): "SCIE/SSCI/AHCI",
    frozenset({"SCIE", "AHCI"}): "SCIE/AHCI",
    frozenset({"SSCI", "AHCI"}): "SSCI/AHCI",
}
AUTO_FIELDS = {"bank", "IF", "jcr", "cas", "top"}
DISPLAY_FIELDS = ("name", "issn", "eissn", "abbr")

# ---------------------------------------------------------------------------
# 简单索引数据源配置表
# 新增索引只需：① 把文件放入 journal/ 目录；② 在此表中追加一行。
#
# 字段说明：
#   label       : 日志显示名称
#   pattern     : glob 模式（在 journal/ 中匹配；有日期则用通配符，否则精确名）
#   name_col    : 期刊名所在列的 0-based 索引
#   issn_col    : ISSN 所在列（可选，用于优先按 ISSN 匹配）
#   field       : 写入 journals.json 的字段名
#   static_value: 静态写入值（value_col 为 None 时使用）
#   value_col   : 动态取值的列索引（None=使用 static_value）
#   stat_key    : 统计输出的键名（None=自动从 label 生成）
#   value_transform: 动态值预处理函数（可选）
# ---------------------------------------------------------------------------
def _normalize_cn_display(name: str) -> str:
    """规范化中文期刊名的显示格式（全局，应用于所有来源）：
    - '主刊名. 副标题' / '主刊名.副标题'（点后紧跟或隔空中文）→ '主刊名（副标题）'
    - '主刊名(中文副标题)' → '主刊名（中文副标题）'（半角括号含中文 → 全角）
    仅处理含中文字符的名称，英文期刊名中的合法点号不受影响。
    """
    if not re.search(r"[\u4e00-\u9fff]", name):
        return name
    # 点号（可含前后空格）后紧跟中文 → 全角括号副标题
    m = re.match(r"^(.+?)\s*\.\s*(?=[\u4e00-\u9fff・])(.+)$", name)
    if m:
        name = f"{m.group(1).rstrip()}（{m.group(2).strip()}）"
    # 半角括号包裹中文内容 → 全角括号
    name = re.sub(
        r"\(\s*([\u4e00-\u9fff][^)]*?)\s*\)",
        lambda mo: f"（{mo.group(1)}）",
        name,
    )
    return name


def _parse_njubs_cn_value(raw: object) -> int | None:
    mapping = {"一流": 1, "权威": 2}
    return mapping.get(str(raw or "").strip())


def _parse_njubs_en_value(raw: object) -> int | None:
    value = parse_int(raw)
    return value if value in (1, 2, 3) else None


# NJUBS-EN Subject Area → integer code（与 src/shared/types.ts 中 NJUBS_SA_LABELS 保持同步）
NJUBS_SA_MAP: dict[str, int] = {
    "Accounting":                                    1,
    "Business":                                      2,
    "Economics":                                     3,
    "Entrep":                                        4,
    "F&A":                                           5,
    "Finance":                                       6,
    "Gen & Strat":                                   7,
    "IB":                                            8,
    "Innovation":                                    9,
    "MIS, KM":                                      10,
    "Management":                                   11,
    "Marketing":                                    12,
    "OR,MS,POM":                                    13,
    "OS/OB,HRM/IR":                                14,
    "PUBLIC, ENVIRONMENTAL & OCCUPATIONAL HEALTH": 15,
    "Statistics":                                   16,
    "Tourism":                                      17,
}


def _parse_njubs_sa(raw: object) -> int | None:
    return NJUBS_SA_MAP.get(str(raw or "").strip())


def _parse_swufe_value(raw: object) -> int | None:
    mapping = {"A+(TOP)": 1, "A+": 2, "A": 3, "A1": 4, "A2": 5}
    return mapping.get(str(raw or "").strip())


def _parse_sufe_soe_value(raw: object) -> int | None:
    mapping = {"顶级": 1, "一类": 2, "二类": 3, "三类": 4}
    return mapping.get(str(raw or "").strip())


def _parse_fdu_som_value(raw: object) -> int | None:
    mapping = {"A+": 1, "A": 2, "A-": 3, "B": 4}
    return mapping.get(str(raw or "").strip())


def _parse_cscd_value(raw: object) -> int | None:
    mapping = {"核心库": 1, "扩展库": 2}
    return mapping.get(str(raw or "").strip())


class _IndexSpec:
    __slots__ = (
        "label",
        "pattern",
        "name_col",
        "issn_col",
        "field",
        "static_value",
        "value_col",
        "stat_key",
        "name_transform",
        "value_transform",
        "extra_col",
        "extra_field",
        "extra_value_transform",
    )

    def __init__(
        self,
        label: str,
        pattern: str,
        name_col: int,
        field: str,
        issn_col: int | None = None,
        static_value: object = True,
        value_col: int | None = None,
        stat_key: str | None = None,
        name_transform: "Callable[[str], str] | None" = None,  # noqa: F821
        value_transform: "Callable[[object], object | None] | None" = None,  # noqa: F821
        extra_col: int | None = None,
        extra_field: str | None = None,
        extra_value_transform: "Callable[[object], object | None] | None" = None,  # noqa: F821
    ) -> None:
        self.label = label
        self.pattern = pattern
        self.name_col = name_col
        self.issn_col = issn_col
        self.field = field
        self.static_value = static_value
        self.value_col = value_col
        self.stat_key = stat_key or (label.lower().replace(" ", "_") + "_rows")
        self.name_transform = name_transform
        self.value_transform = value_transform
        self.extra_col = extra_col
        self.extra_field = extra_field
        self.extra_value_transform = extra_value_transform


SIMPLE_INDEX_SOURCES: list[_IndexSpec] = [
    # 经管专业期刊列表（纯名单，无 ISSN，按刊名匹配）
    _IndexSpec("UTD24",   "UTD24.xlsx",       0, "utd24", static_value=True),
    _IndexSpec("FT50",    "FT50.xlsx",        0, "ft50",  static_value=True),
    # AJG/ABS 等级：第 0 列=期刊名，第 1 列=等级（1/2/3/4/4*）
    _IndexSpec("AJG",     "AJG*.xlsx",        0, "abs",   value_col=1),
    # 中文数据库（按中文刊名匹配）
    # 北核用 "." 代替括号分隔副标题，name_transform 统一为 "（）" 后再匹配/存储
    _IndexSpec("北核",    "北核*.xlsx",       5, "pku",   static_value=True),
    _IndexSpec("CSSCI",   "CSSCI_*.xlsx",       1, "cssci", static_value=1),
    _IndexSpec("CSSCI扩", "CSSCI扩展版_*.xlsx", 1, "cssci", static_value=2),
    # 高校期刊目录
    _IndexSpec("NJUBS中", "NJUBS_CN_*.xlsx",  0, "njubs_cn", value_col=1, value_transform=_parse_njubs_cn_value),
    _IndexSpec("NJUBS英", "NJUBS_EN_*.xlsx",  3, "njubs_en", issn_col=2, value_col=4, value_transform=_parse_njubs_en_value,
               extra_col=1, extra_field="njubs_sa", extra_value_transform=_parse_njubs_sa),
    # 高校专业期刊目录
    _IndexSpec("SWUFE",   "SWUFE_*.xlsx",     3, "swufe",    issn_col=4, value_col=5, value_transform=_parse_swufe_value),
    _IndexSpec("SUFE SOE","SUFE SOE_*.xlsx",  2, "sufe_soe", value_col=3, value_transform=_parse_sufe_soe_value),
    _IndexSpec("FDU SOM", "FDU SOM_*.xlsx",   1, "fdu_som",  value_col=2, value_transform=_parse_fdu_som_value),
    _IndexSpec("CSCD",    "CSCD_*.xlsx",       1, "cscd",     issn_col=2, value_col=3, value_transform=_parse_cscd_value),
]
OPTIONAL_FIELDS = (
    "IF",
    "bank",
    "jcr",
    "cas",
    "top",
    "ei",
    "cscd",
    "pku",
    "sos",
    "utd24",
    "ft50",
    "abs",
    "cssci",
    "njubs_cn",
    "njubs_en",
    "njubs_sa",
    "cnki_if",
    "cnki_ifs",
    "swufe",
    "sufe_soe",
    "fdu_som",
)


def normalize_name(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"&amp;(amp;)?|&", "and", text, flags=re.IGNORECASE)
    text = text.replace("英文版", "英文").replace("学版", "学")
    text = re.sub(r"^the\s+", "", text, flags=re.IGNORECASE)
    return re.sub(r"[\s\u00a0\-_–—.,、:;()（）·•*，：；+®/\"'<>]+", "", text)


def normalize_issn(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text in {"", "N/A", "NA", "NONE", "NULL", "-", "--", "NAN"}:
        return None
    return text or None


def parse_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).strip()))
    except ValueError:
        return None


def parse_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).strip()), 3)
    except ValueError:
        return None


def choose_latest_file(folder: Path, pattern: str) -> Path:
    matches = list(folder.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"未找到匹配文件: {folder / pattern}")

    def sort_key(path: Path) -> tuple[str, int, str]:
        digits = "".join(re.findall(r"\d+", path.stem))
        return digits, int(path.stat().st_mtime), path.name

    return max(matches, key=sort_key)


def col_to_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1


def iter_xlsx_rows(path: Path) -> Iterable[list[object | None]]:
    with ZipFile(path) as zf:
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            shared_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in shared_root.findall("main:si", XLSX_NS):
                parts = [node.text or "" for node in item.iterfind(".//main:t", XLSX_NS)]
                shared_strings.append("".join(parts))

        workbook_root = ET.fromstring(zf.read("xl/workbook.xml"))
        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels_root.findall("package:Relationship", XLSX_NS)
        }

        first_sheet = workbook_root.find("main:sheets/main:sheet", XLSX_NS)
        if first_sheet is None:
            return

        rel_id = first_sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        target = rel_map[rel_id].lstrip("/")
        sheet_path = target if target.startswith("xl/") else f"xl/{target}"
        sheet_root = ET.fromstring(zf.read(sheet_path))

        for row in sheet_root.findall(".//main:sheetData/main:row", XLSX_NS):
            values: list[object | None] = []
            next_index = 0

            for cell in row.findall("main:c", XLSX_NS):
                index = col_to_index(cell.attrib.get("r", "A1"))
                while next_index < index:
                    values.append(None)
                    next_index += 1

                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    text = "".join(node.text or "" for node in cell.findall(".//main:t", XLSX_NS))
                    values.append(text)
                else:
                    raw_node = cell.find("main:v", XLSX_NS)
                    raw_text = raw_node.text if raw_node is not None else None
                    if raw_text is None:
                        values.append(None)
                    elif cell_type == "s":
                        values.append(shared_strings[int(raw_text)])
                    else:
                        values.append(raw_text)
                next_index += 1

            yield values


def name_quality(name: str) -> tuple[int, int, int]:
    """返回名称质量元组（越大越好），用于在多来源名称间择优。
    维度 1：有中文 > 纯字母且首字母大写 > 全大写缩写 > 纯数字/空
    维度 2：对中文名，有全角括号「（）」优于用点「.」分隔副标题
    维度 3：对中文名，名称越短越好（避免全称冗长）；英文名越长越好
    """
    if not name:
        return (-1, 0, 0)
    if re.search(r"[\u4e00-\u9fff]", name):
        # 全角括号版优于点分隔版（显示更规范）
        bracket_bonus = 1 if "（" in name or "）" in name else 0
        return (3, bracket_bonus, -len(name))

    letters = re.sub(r"[^A-Za-z]+", "", name)
    if not letters:
        return (1, 0, -len(name))
    if letters.isupper():
        return (1, 0, -len(name))
    return (2, 0, -len(name))


def prefer_name(current: str, candidate: str) -> str:
    candidate = _normalize_cn_display(str(candidate or "").strip())
    current = _normalize_cn_display(str(current or "").strip())
    if not current:
        return candidate
    if not candidate:
        return current
    if name_quality(candidate) > name_quality(current):
        return candidate
    return current


def prefer_jcr(current_if: float | None, current_q: int | None, new_if: float | None, new_q: int | None) -> tuple[float | None, int | None]:
    if current_q is None:
        return new_if, new_q
    if new_q is None:
        return current_if, current_q
    if new_q < current_q:
        return new_if, new_q
    if new_q == current_q and (new_if or 0) > (current_if or 0):
        return new_if, new_q
    return current_if, current_q


def prefer_cas(current_zone: int | None, current_top: bool, new_zone: int | None, new_top: bool) -> tuple[int | None, bool]:
    if current_zone is None:
        return new_zone, new_top
    if new_zone is None:
        return current_zone, current_top
    return min(current_zone, new_zone), current_top or new_top


def default_record() -> dict:
    return {
        "name": "",
        "issn": "",
        "eissn": "",
        "abbr": "",
        "IF": None,
        "jcr": None,
        "cas": None,
        "top": False,
        "_banks": set(),
        "_title_keys": set(),
    }


def create_catalog() -> dict:
    return {
        "records": {},
        "issn_index": {},
        "title_index": {},
        "next_id": 1,
        "stats": {
            "wos_rows": 0,
            "jcr_rows": 0,
            "cas_rows": 0,
            "overrides": 0,
        },
    }


def record_ids_for(catalog: dict, name: str = "", issn: str | None = None, eissn: str | None = None) -> set[int]:
    """查找匹配指定 ISSN / eISSN / 刊名的所有记录 ID。
    ISSN 与刊名均参与查找（不提前返回），避免"ISSN 匹配到记录 A、
    刊名匹配到记录 B"时遗漏合并（如 SWUFE 存 MIT Sloan Management Review、
    FT50 存 Sloan Management Review 各自独立创建记录的情形）。"""
    ids: set[int] = set()
    for key in (issn, eissn):
        if key and key in catalog["issn_index"]:
            record_id = catalog["issn_index"][key]
            if record_id in catalog["records"]:
                ids.add(record_id)
            else:
                del catalog["issn_index"][key]

    title_key = normalize_name(name)
    if title_key and title_key in catalog["title_index"]:
        record_id = catalog["title_index"][title_key]
        if record_id in catalog["records"]:
            ids.add(record_id)
        else:
            del catalog["title_index"][title_key]
    return ids


def register_aliases(catalog: dict, record_id: int, record: dict) -> None:
    for key in (record.get("issn"), record.get("eissn")):
        if key:
            catalog["issn_index"][key] = record_id
    for key in record.get("_title_keys", set()):
        if key:
            catalog["title_index"][key] = record_id


def merge_record_pair(base: dict, incoming: dict) -> dict:
    base["name"] = prefer_name(base.get("name", ""), incoming.get("name", ""))
    if not base.get("issn"):
        base["issn"] = incoming.get("issn", "")
    if not base.get("eissn"):
        base["eissn"] = incoming.get("eissn", "")
    if not base.get("abbr"):
        base["abbr"] = incoming.get("abbr", "")

    base["IF"], base["jcr"] = prefer_jcr(
        base.get("IF"),
        base.get("jcr"),
        incoming.get("IF"),
        incoming.get("jcr"),
    )
    base["cas"], base["top"] = prefer_cas(
        base.get("cas"),
        bool(base.get("top")),
        incoming.get("cas"),
        bool(incoming.get("top")),
    )

    for field in ("ei", "cscd", "pku", "sos", "utd24", "ft50", "abs", "cssci", "njubs_cn", "njubs_en", "njubs_sa",
                  "swufe", "sufe_soe", "fdu_som"):
        if field in incoming:
            base[field] = incoming[field]

    base["_banks"].update(incoming.get("_banks", set()))
    base["_title_keys"].update(incoming.get("_title_keys", set()))
    return base


def merge_records(catalog: dict, record_ids: set[int]) -> int:
    master_id = min(record_ids)
    master = catalog["records"][master_id]

    for record_id in sorted(record_ids):
        if record_id == master_id:
            continue
        incoming = catalog["records"].pop(record_id)
        merge_record_pair(master, incoming)

    register_aliases(catalog, master_id, master)
    return master_id


def resolve_record(catalog: dict, name: str = "", issn: str | None = None, eissn: str | None = None) -> tuple[int, dict]:
    record_ids = record_ids_for(catalog, name=name, issn=issn, eissn=eissn)

    if not record_ids:
        record_id = catalog["next_id"]
        catalog["next_id"] += 1
        catalog["records"][record_id] = default_record()
    else:
        record_id = merge_records(catalog, record_ids)

    record = catalog["records"][record_id]

    record["name"] = prefer_name(record.get("name", ""), name)
    if issn and not record.get("issn"):
        record["issn"] = issn
    if eissn and not record.get("eissn"):
        record["eissn"] = eissn

    title_key = normalize_name(name)
    if title_key:
        record["_title_keys"].add(title_key)
    register_aliases(catalog, record_id, record)
    return record_id, record


def apply_wos_sources(catalog: dict, source_dir: Path) -> None:
    for bank in WOS_BANKS:
        csv_path = choose_latest_file(source_dir, f"{bank}_*.csv")
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                title = (row.get("Journal title") or "").strip()
                issn = normalize_issn(row.get("ISSN"))
                eissn = normalize_issn(row.get("eISSN"))
                _, record = resolve_record(catalog, name=title, issn=issn, eissn=eissn)
                record["_banks"].add(bank)
                catalog["stats"]["wos_rows"] += 1


def apply_jcr_source(catalog: dict, source_dir: Path) -> None:
    xlsx_path = choose_latest_file(source_dir, "JCR_*.xlsx")
    rows = iter_xlsx_rows(xlsx_path)
    next(rows, None)

    for row in rows:
        if not row:
            continue
        title = str(row[0] or "").strip()
        issn = normalize_issn(row[1] if len(row) > 1 else None)
        eissn = normalize_issn(row[2] if len(row) > 2 else None)
        category = str(row[3] or "").strip()
        jif = parse_float(row[5] if len(row) > 5 else None)
        quartile_text = str(row[6] or "").strip().upper()
        quartile = parse_int(quartile_text[1:]) if quartile_text.startswith("Q") else None

        _, record = resolve_record(catalog, name=title, issn=issn, eissn=eissn)
        record["IF"], record["jcr"] = prefer_jcr(record.get("IF"), record.get("jcr"), jif, quartile)

        bank_match = re.search(r"\((SCIE|SSCI|ESCI|AHCI)\)", category)
        if bank_match:
            record["_banks"].add(bank_match.group(1))

        catalog["stats"]["jcr_rows"] += 1


def _parse_abs_value(raw: object) -> "int | str | None":
    """将 AJG/ABS 原始等级转为标准值：1/2/3/4/4* 或 None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "4*":
        return "4*"
    v = parse_int(s)
    return v if v in (1, 2, 3, 4) else None


def apply_simple_index(catalog: dict, source_dir: Path, spec: _IndexSpec) -> int:
    """通用简单索引加载器：读取 xlsx，按刊名匹配，写入指定字段。"""
    try:
        path = choose_latest_file(source_dir, spec.pattern)
    except FileNotFoundError:
        print(f"  [跳过] 未找到 {spec.label} 数据文件（{spec.pattern}）")
        return 0

    rows = iter_xlsx_rows(path)
    next(rows, None)  # 跳过表头

    count = 0
    for row in rows:
        if not row or len(row) <= spec.name_col:
            continue
        name = str(row[spec.name_col] or "").strip()
        if not name:
            continue
        if spec.name_transform:
            name = spec.name_transform(name)
        issn = normalize_issn(row[spec.issn_col] if spec.issn_col is not None and len(row) > spec.issn_col else None)

        if spec.value_col is not None:
            raw_val = row[spec.value_col] if len(row) > spec.value_col else None
            if spec.field == "abs":
                value: object = _parse_abs_value(raw_val)
            else:
                value = raw_val
            if spec.value_transform:
                value = spec.value_transform(value)
            if value is None:
                continue
        else:
            value = spec.static_value

        _, record = resolve_record(catalog, name=name, issn=issn)
        existing = record.get(spec.field)
        # cssci：取较优值（1=核心 > 2=扩展），不降级
        if spec.field == "cssci" and existing is not None and isinstance(value, int) and isinstance(existing, int):
            record[spec.field] = min(existing, value)
        else:
            record[spec.field] = value
        # 附加字段（如 njubs_sa）
        if spec.extra_col is not None and spec.extra_field:
            raw_extra = row[spec.extra_col] if len(row) > spec.extra_col else None
            extra_val: object = raw_extra
            if spec.extra_value_transform:
                extra_val = spec.extra_value_transform(raw_extra)
            if extra_val is not None:
                record[spec.extra_field] = extra_val
        count += 1

    return count


def apply_ei_source(catalog: dict, source_dir: Path) -> int:
    """从 EI_*.xlsx 中读取 EI 收录期刊。
    语言（第 12 列）含 CHINESE 时优先使用中文刊名（第 4 列）。
    仅处理 Source Type == 'Journal' 的行（第 3 列）。
    """
    try:
        path = choose_latest_file(source_dir, "EI_*.xlsx")
    except FileNotFoundError:
        print("  [跳过] 未找到 EI 数据文件（EI_*.xlsx）")
        return 0

    rows = iter_xlsx_rows(path)
    next(rows, None)  # 跳过表头

    count = 0
    for row in rows:
        if not row or len(row) < 13:
            continue
        source_type = str(row[3] or "").strip()
        if source_type != "Journal":
            continue

        lang = str(row[12] or "").strip().upper()
        is_chinese = "CHINESE" in lang

        std_title = str(row[1] or "").strip()
        cn_title = str(row[4] or "").strip() if len(row) > 4 else ""
        name = cn_title if (is_chinese and cn_title) else std_title
        if not name:
            continue

        issn = normalize_issn(row[8] if len(row) > 8 else None)
        eissn = normalize_issn(row[9] if len(row) > 9 else None)

        record_id, record = resolve_record(catalog, name=name, issn=issn, eissn=eissn)
        # 也将标准英文名注册为别名，便于后续查找
        if is_chinese and std_title:
            std_key = normalize_name(std_title)
            if std_key:
                record["_title_keys"].add(std_key)
                catalog["title_index"][std_key] = record_id
        record["ei"] = True
        count += 1

    return count


def apply_derived_university_indexes(catalog: dict) -> None:
    """补齐高校期刊目录的兜底档位。
    中文：所有未进入前序 NJUBS 中文档位的 CSSCI 期刊 → 核心（3）
    英文：所有未进入前序 NJUBS 英文档位的 SSCI 期刊 → 4区（4）
    """
    for record in catalog["records"].values():
        if record.get("njubs_cn") is None and record.get("cssci") is not None:
            record["njubs_cn"] = 3

        banks = set(record.get("_banks", set()))
        bank_text = str(record.get("bank") or "")
        has_ssci = "SSCI" in banks or "SSCI" in bank_text
        if record.get("njubs_en") is None and has_ssci:
            record["njubs_en"] = 4


def apply_cnki_sources(catalog: dict, source_dir: Path) -> int:
    """从 CNKI_自科_*.xlsx 和 CNKI_社科_*.xlsx 中读取复合影响因子与综合影响因子。
    刊名在第 3 列（0-based），仅处理 行类型=='期刊' 的数据行。
    复合影响因子：第 6 列；期刊综合影响因子：第 12 列。
    """
    count = 0
    for pattern in ("CNKI_自科_*.xlsx", "CNKI_社科_*.xlsx"):
        try:
            path = choose_latest_file(source_dir, pattern)
        except FileNotFoundError:
            print(f"  [跳过] 未找到 {pattern}")
            continue

        rows = iter_xlsx_rows(path)
        next(rows, None)  # 跳过表头

        for row in rows:
            if not row or len(row) < 13:
                continue
            row_type = str(row[1] or "").strip()
            if row_type != "期刊":
                continue
            name = str(row[3] or "").strip()
            if not name:
                continue
            cnki_if = parse_float(row[12])   # 综合影响因子（仅期刊引用）
            cnki_ifs = parse_float(row[6])   # 复合影响因子（含学位论文等，来源更广）
            if cnki_if is None and cnki_ifs is None:
                continue

            _, record = resolve_record(catalog, name=name)
            if cnki_if is not None:
                record["cnki_if"] = cnki_if
            if cnki_ifs is not None:
                record["cnki_ifs"] = cnki_ifs
            count += 1

    return count


def apply_cas_source(catalog: dict, source_dir: Path) -> None:
    xlsx_path = choose_latest_file(source_dir, "CAS_*.xlsx")
    rows = iter_xlsx_rows(xlsx_path)
    next(rows, None)

    for row in rows:
        if not row:
            continue
        title = str(row[0] or "").strip()
        zone = parse_int(row[1] if len(row) > 1 else None)
        top = str(row[2] or "").strip() == "是"
        _, record = resolve_record(catalog, name=title)
        record["cas"], record["top"] = prefer_cas(record.get("cas"), bool(record.get("top")), zone, top)
        catalog["stats"]["cas_rows"] += 1


def load_overrides(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def apply_overrides(catalog: dict, overrides: list[dict]) -> None:
    for item in overrides:
        name = str(item.get("name") or "").strip()
        issn = normalize_issn(item.get("issn"))
        eissn = normalize_issn(item.get("eissn"))
        record_id, record = resolve_record(catalog, name=name, issn=issn, eissn=eissn)

        for field in DISPLAY_FIELDS + OPTIONAL_FIELDS:
            if field in item:
                if field == "name":
                    record["name"] = item[field]
                    title_key = normalize_name(item[field])
                    if title_key:
                        record["_title_keys"].add(title_key)
                else:
                    record[field] = item[field]

        register_aliases(catalog, record_id, record)
        catalog["stats"]["overrides"] += 1


def bank_value_from_set(values: set[str]) -> str | None:
    if not values:
        return None
    normalized = set(values)
    if "ESCI" in normalized and len(normalized) > 1:
        normalized.discard("ESCI")
    return SUPPORTED_BANKS.get(frozenset(normalized))


def finalize_record(record: dict) -> dict:
    bank_value = bank_value_from_set(record.get("_banks", set()))
    item: dict = {}

    for field in DISPLAY_FIELDS:
        value = record.get(field)
        if value not in (None, ""):
            item[field] = value

    for field in OPTIONAL_FIELDS:
        value = record.get(field)
        if field == "bank":
            value = bank_value

        if value in (None, "", False):
            continue
        item[field] = value

    return item


def build_catalog(source_dir: Path, overrides: list[dict]) -> tuple[list[dict], dict]:
    catalog = create_catalog()
    apply_wos_sources(catalog, source_dir)
    apply_jcr_source(catalog, source_dir)
    apply_cas_source(catalog, source_dir)
    catalog["stats"]["cnki_rows"] = apply_cnki_sources(catalog, source_dir)
    catalog["stats"]["ei_rows"] = apply_ei_source(catalog, source_dir)
    for spec in SIMPLE_INDEX_SOURCES:
        catalog["stats"][spec.stat_key] = apply_simple_index(catalog, source_dir, spec)
    apply_overrides(catalog, overrides)
    apply_derived_university_indexes(catalog)

    journals = [finalize_record(record) for record in catalog["records"].values()]
    journals = [item for item in journals if item.get("name")]
    journals.sort(key=lambda item: (normalize_name(item.get("name", "")), item.get("issn", ""), item.get("eissn", "")))

    unsupported_banks = sum(
        1
        for record in catalog["records"].values()
        if record.get("_banks") and bank_value_from_set(record["_banks"]) is None
    )

    stats = dict(catalog["stats"])
    stats["records"] = len(journals)
    stats["unsupported_banks"] = unsupported_banks
    return journals, stats


def sync_journals(data_dir: Path | None = None, source_dir: Path | None = None, dry_run: bool = False) -> dict:
    script_dir = Path(__file__).resolve().parent
    data_dir = data_dir or script_dir
    source_dir = source_dir or script_dir / "journal"

    journals_path = data_dir / "journals.json"
    overrides_path = data_dir / "manual-overrides.json"
    overrides = load_overrides(overrides_path)
    journals, stats = build_catalog(source_dir, overrides)

    if not dry_run:
        journals_path.write_text(json.dumps(journals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n✓ 全量目录同步{'(dry-run)' if dry_run else ''}完成")
    print(f"  生成期刊: {stats['records']}")
    print(f"  WoS 行数: {stats['wos_rows']}")
    print(f"  JCR 行数: {stats['jcr_rows']}")
    print(f"  CAS 行数: {stats['cas_rows']}")
    if stats.get("cnki_rows"):
        print(f"  CNKI    : {stats['cnki_rows']} 条")
    if stats.get("ei_rows"):
        print(f"  EI      : {stats['ei_rows']} 条")
    for spec in SIMPLE_INDEX_SOURCES:
        count = stats.get(spec.stat_key, 0)
        if count:
            print(f"  {spec.label:8s}: {count} 条")
    print(f"  手工覆盖: {stats['overrides']}")
    if stats["unsupported_banks"]:
        print(f"  未支持的 WoS 组合: {stats['unsupported_banks']} 条（已忽略 bank 字段）")

    return {
        "journals": journals,
        "stats": stats,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="从 journal/ 原始索引表重建完整的 journals.json")
    parser.add_argument("--dry-run", action="store_true", help="只生成统计结果，不写入 journals.json")
    args = parser.parse_args()
    sync_journals(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
