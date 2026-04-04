# ScholarX 期刊数据维护指南

## 文件说明

| 文件 | 说明 |
|---|---|
| `journal/` | 原始索引源文件目录（所有 xlsx/csv 放这里，不入仓库） |
| `sync_indexes.py` | 从 `journal/` 重建全量 `journals.json` |
| `build-data.py` | 将 `journals.json` 编码为扩展可读的 `odata.json` 和 `journal-db.json` |
| `enrich-issn.py` | 为缺失 ISSN 的期刊自动查询补全（CrossRef / OpenAlex） |
| `enrich-abbr.py` | 为缺失缩写的期刊自动查询补全（NLM Catalog） |
| `journals.json` | 自动生成的全量期刊目录，可查阅，不建议手工编辑 |
| `manual-overrides.json` | 手工维护的补充/修正项，**唯一需要日常编辑的文件** |
| `odata.json` | 构建产物，上传到 GitHub/Gitee 作为远端数据源 |

---

## 日常更新流程

### 更新期刊数据（换新年份文件）

1. 把新年份文件放入 `journal/`，文件名保持现有风格（见下表）
2. 运行一条命令：

```bash
python build-data.py --sync
```

这会自动完成：读取所有最新源文件 → 生成 `journals.json` → 应用 `manual-overrides.json` → 输出 `odata.json` 和 `src/data/journal-db.json`。

仅预览同步结果，不写入任何文件：

```bash
python sync_indexes.py --dry-run
```

### 补全缺失字段（可选，耗时较长）

```bash
# 补全缺失 ISSN（CrossRef / OpenAlex，~400 条，需数分钟）
python enrich-issn.py

# 补全缺失缩写（NLM Catalog 批量查询，~13000 条，需 20-30 分钟）
python enrich-abbr.py

# 两个脚本均支持断点续跑（缓存文件 .enrich-*-cache.json）
# 完成后自动调用 build-data.py --sync 重建数据库
```

### 上传数据源

把生成的 `odata.json` 推送到此仓库，然后在扩展的 `src/data/journal-loader.ts` 中配置：

```ts
const DATA_VERSION = 'jt_260404';  // 每次大规模更新时修改，触发用户端重拉
const DATA_SOURCES = [
  'https://raw.githubusercontent.com/Taboo725/journal-ranking/master/odata.json',
];
```

---

## 当前数据源一览

所有源文件都放在 `journal/` 目录（不入仓库）。

### WoS 收录数据（CSV）

| 文件命名规范 | 字段 | 说明 |
|---|---|---|
| `SCIE_YYYYMMDD.csv` | `bank` | SCI 扩展版收录 |
| `SSCI_YYYYMMDD.csv` | `bank` | 社会科学引文索引 |
| `ESCI_YYYYMMDD.csv` | `bank` | 新兴资源引文索引 |
| `AHCI_YYYYMMDD.csv` | `bank` | 艺术与人文引文索引 |

必需列名：`Journal title`、`ISSN`、`eISSN`（表头行，大小写敏感）。

### 分区与影响因子数据（XLSX）

| 文件命名规范 | 字段 | 说明 |
|---|---|---|
| `JCR_YYYY.xlsx` | `IF`、`jcr` | 影响因子与 JCR 分区（Q1~Q4） |
| `CAS_YYYY.xlsx` | `cas`、`top` | 中科院分区与 Top 期刊标记 |
| `CNKI_自科_YYYY.xlsx` | `cnki_if`、`cnki_ifs` | CNKI 自然科学综合/复合影响因子 |
| `CNKI_社科_YYYY.xlsx` | `cnki_if`、`cnki_ifs` | CNKI 社会科学综合/复合影响因子 |

**JCR 列顺序**（0-based）：0=刊名, 1=ISSN, 2=eISSN, 3=类别, 5=IF, 6=分区（Q1~Q4）。  
**CAS 列顺序**：0=刊名, 1=区号（1~4）, 2=是否 Top（"是"/"否"）。  
**CNKI 列顺序**：2=刊名, 6=复合影响因子（cnki_ifs）, 12=综合影响因子（cnki_if）；仅保留 `行类型=="期刊"` 的行。

### 简单索引（XLSX，名单型）

| 文件命名规范 | 字段 | 值 | 名列 | 说明 |
|---|---|---|---|---|
| `UTD24.xlsx` | `utd24` | `true` | 0 | UTD24 核心期刊 |
| `FT50.xlsx` | `ft50` | `true` | 0 | FT50 期刊 |
| `AJG (ABS)_YYYY.xlsx` | `abs` | 1/2/3/4/4* | 0（名）, 1（等级）| ABS/AJG 分级 |
| `北核_YYYY.xlsx` | `pku` | `true` | 5 | 北大核心（中文刊名列在第 6 列） |
| `CSSCI.xlsx` | `cssci` | `1` | 1 | CSSCI 核心期刊 |
| `CSSCI扩展版.xlsx` | `cssci` | `2` | 1 | CSSCI 扩展版 |

> **命名规范**：有年份的文件用 `*` 通配（脚本自动选最新），无年份的精确匹配。

---

## 新增一个索引数据源

以后每加一类索引，只需两步：

### 第一步：把文件放入 `journal/`

文件命名建议：
- 有年份版本：`索引名_YYYY.xlsx`（如 `ESCI_20260401.csv`）
- 无版本区分：精确文件名（如 `UTD24.xlsx`）

### 第二步：在 `sync_indexes.py` 的 `SIMPLE_INDEX_SOURCES` 追加一行

```python
SIMPLE_INDEX_SOURCES: list[_IndexSpec] = [
    # 已有条目...

    # 新增示例 1：布尔型名单（刊名在第 0 列）
    _IndexSpec("EI",    "EI_*.xlsx",  0, "ei",   True),

    # 新增示例 2：等级值（刊名在第 0 列，等级在第 1 列）
    _IndexSpec("ABDC",  "ABDC*.xlsx", 0, "abdc", value_col=1),
]
```

`_IndexSpec` 参数说明：

| 参数 | 类型 | 说明 |
|---|---|---|
| `label` | str | 日志中显示的名称 |
| `pattern` | str | glob 文件名模式（在 `journal/` 下匹配） |
| `name_col` | int | 期刊名所在列（0-based） |
| `field` | str | 写入 journals.json 的字段名 |
| `static_value` | any | 写入的固定值（默认 `True`） |
| `value_col` | int\|None | 从该列动态读值（None 则用 static_value） |
| `stat_key` | str\|None | 统计输出键名（None 则自动生成） |
| `name_transform` | callable\|None | 期刊名预处理函数（用于格式修正） |

### 第三步（若是新字段）：在 `build-data.py` 的 `build_info()` 中加编码

```python
def build_info(j: dict) -> dict:
    ...
    if j.get("ei"):
        info["F"] = 1
```

已使用的压缩键：

| 键 | 字段 | 说明 |
|---|---|---|
| `A` | IF | 影响因子 |
| `C` | jcr | JCR 分区 |
| `CA` | bank | WoS 收录类型 |
| `D` | cas | 中科院分区（+10 表示 Top） |
| `E` | top | 中科院 Top 期刊 |
| `F` | ei | EI 收录 |
| `G` | cscd | CSCD 核心库/扩展库 |
| `H` | pku | 北大核心 |
| `I` | utd24 | UTD24 核心期刊 |
| `J` | ft50 | FT50 期刊 |
| `K` | abs | ABS/AJG 等级 |
| `L` | cssci | CSSCI 核心/扩展 |
| `M` | cnki_if | CNKI 综合影响因子（仅期刊引用） |
| `N` | cnki_ifs | CNKI 复合影响因子（含学位论文等） |
| `S` | sos | 预警期刊 |

---

## 字段合并规则

| 字段 | 合并规则 |
|---|---|
| `name` | 按质量择优：中文名 > 首字母大写英文名 > 全大写缩写 |
| `issn`/`eissn` | 先到先得（不覆盖已有值） |
| `IF` / `jcr` | 取分区更优（更小）的来源；同分区取 IF 更高者 |
| `cas` / `top` | 取分区更优（更小）的来源；任一来源标记 Top 则为 Top |
| `cssci` | 取较优值（1=核心 优先于 2=扩展） |
| 其余字段 | 后来覆盖；`manual-overrides.json` 最后应用，优先级最高 |

---

## 名称匹配机制

所有期刊名在建立匹配键时，会经过以下规范化（`normalize_name`）：

1. 转小写
2. `&`/`&amp;` → `and`
3. 去除前缀 `the `
4. 去除所有空白、标点、括号、`.`、`·` 等

因此以下三种写法匹配到同一期刊：

```
北京大学学报（哲学社会科学版）   ← CSSCI 格式
北京大学学报(哲学社会科学版)    ← 半角括号
北京大学学报.哲学社会科学版     ← 北核格式
```

英文期刊同理：`Journal of Finance` = `j. finance` = `THE JOURNAL OF FINANCE`。

---

## 手工覆盖：manual-overrides.json

只放需要人工补充或修正的字段，应用顺序在所有自动源之后。

```json
[
  {
    "name": "Harvard Business Review",
    "issn": "0017-8012",
    "abbr": "HBR",
    "ft50": true,
    "abs": 3
  },
  {
    "name": "经济研究",
    "issn": "0577-9154",
    "cssci": 1,
    "pku": true
  }
]
```

可覆盖的字段：`name`、`issn`、`eissn`、`abbr`、`IF`、`bank`、`jcr`、`cas`、`top`、`ei`、`cscd`、`pku`、`sos`、`utd24`、`ft50`、`abs`、`cssci`、`cnki_if`、`cnki_ifs`。

`sos`（预警期刊）格式：`{"24": 1}` 表示 2024 年高预警（1=高 2=中 3=低 4=引用操纵）。

---

## 字段完整参考

| journals.json 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `name` | string | 期刊全名 | 自动 |
| `issn` | string | 印刷版 ISSN（XXXX-XXXX） | 自动 / enrich-issn |
| `eissn` | string | 电子版 ISSN | 自动 / enrich-issn |
| `abbr` | string | 常用缩写（NLM 格式） | enrich-abbr |
| `IF` | number | 影响因子 | JCR |
| `bank` | string | WoS 收录类型 | WoS CSV / JCR |
| `jcr` | 1~4 | JCR 分区 | JCR |
| `cas` | 1~4 | 中科院分区 | CAS |
| `top` | bool | 中科院 Top 期刊 | CAS |
| `ei` | bool | EI 收录 | 手工 |
| `cscd` | 1 或 2 | CSCD 核心库(1)/扩展库(2) | 手工 |
| `pku` | bool | 北大核心 | 北核.xlsx |
| `cssci` | 1 或 2 | CSSCI 核心(1)/扩展(2) | CSSCI.xlsx |
| `utd24` | bool | UTD24 核心期刊 | UTD24.xlsx |
| `ft50` | bool | FT50 期刊 | FT50.xlsx |
| `abs` | 1/2/3/4/"4*" | ABS/AJG 等级 | AJG.xlsx |
| `cnki_if` | number | CNKI 综合影响因子（仅期刊引用） | CNKI.xlsx |
| `cnki_ifs` | number | CNKI 复合影响因子（含学位论文等） | CNKI.xlsx |
| `sos` | object | 预警期刊信息 | 手工 |
