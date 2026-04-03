# ScholarX 期刊数据维护指南

## 文件说明

| 文件 | 说明 |
|---|---|
| `../journal/` | 原始索引源文件目录，放 `SCIE/SSCI/AHCI/ESCI/JCR/CAS` |
| `manual-overrides.json` | 你真正需要手工维护的小文件，只放人工补充字段 |
| `sync_indexes.py` | 从 `../journal/` 重建全量 `journals.json` |
| `journals.json` | 自动生成的全量期刊目录，可查看，不建议手工编辑 |
| `build-data.py` | 将 `journals.json` 转成扩展可读的 `odata.json` |
| `odata.json` | 构建产物，上传到 GitHub/Gitee 作为数据源 |

---

## 现在的推荐流程

以后更新期刊目录，只需要：

```bash
# 在 data-source/ 目录下
python build-data.py --sync
```

这条命令会自动：

1. 读取 `../journal/` 中最新的 `SCIE_*.csv / SSCI_*.csv / AHCI_*.csv / ESCI_*.csv`
2. 读取最新的 `JCR_*.xlsx / CAS_*.xlsx`
3. 生成全量 `journals.json`
4. 把 `manual-overrides.json` 里的人工补充项覆盖进去
5. 生成 `odata.json`

如果你只想预览同步结果，不写回 `journals.json`：

```bash
python sync_indexes.py --dry-run
```

---

## 你应该编辑哪个文件

不要再手工维护全量 `journals.json`。

以后只编辑 `manual-overrides.json`，例如：

```json
[
  {
    "name": "Harvard Business Review",
    "issn": "0017-8012",
    "abbr": "Harv Bus Rev",
    "ft50": true,
    "abs": 3
  },
  {
    "name": "经济研究",
    "issn": "0577-9154",
    "abbr": "经济研究",
    "pku": true,
    "cssci": 1
  }
]
```

适合放在 `manual-overrides.json` 里的字段：

- `abbr`
- `ei`
- `cscd`
- `pku`
- `sos`
- `utd24`
- `ft50`
- `abs`
- `cssci`
- 以及少数需要人工修正的 `name / issn / eissn / bank / IF / jcr / cas / top`

---

## 自动生成了哪些字段

脚本会优先从源文件自动生成这些字段：

- `bank`
- `IF`
- `jcr`
- `cas`
- `top`

其中：

- `SCIE/SSCI/AHCI/ESCI` 来自 WoS CSV 与 JCR 分类
- `IF / jcr` 来自 `JCR_*.xlsx`
- `cas / top` 来自 `CAS_*.xlsx`

---

## 更新新的索引文件

以后换新年份时，只需要：

1. 把新的源文件放进仓库根目录的 `journal/`
2. 文件名保持现有风格，例如 `SCIE_20260401.csv`、`JCR_2026.xlsx`
3. 运行 `python build-data.py --sync`

脚本会自动选择同类文件中“最新”的那一份。

---

## 配置数据源 URL

上传 `odata.json` 后，打开 `src/data/journal-loader.ts`，修改 `DATA_SOURCES`：

```ts
const DATA_SOURCES = [
  'https://gitee.com/你的用户名/你的仓库/raw/main/odata.json',
  'https://raw.githubusercontent.com/你的用户名/你的仓库/main/odata.json',
];
```

---

## 更新数据版本

每次大规模更新目录后，顺手修改 `src/data/journal-loader.ts` 里的 `DATA_VERSION`，这样用户端会重新拉取：

```ts
const DATA_VERSION = 'jt_260404';
```
