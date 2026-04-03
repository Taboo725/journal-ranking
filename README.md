# ScholarX 期刊数据维护指南

## 文件说明

| 文件 | 说明 |
|---|---|
| `journals.json` | **你只需要编辑这个文件**，人类可读格式，每条记录一个期刊 |
| `build-data.py` | 构建脚本，将 journals.json 转换为扩展可读的 odata.json |
| `odata.json` | 构建产物，上传到 git 仓库作为数据源（不要手动编辑） |

---

## 添加/修改期刊

打开 `journals.json`，每条记录格式如下：

```json
{
  "name":  "Journal of the American Chemical Society",  ← 期刊全名（必填）
  "issn":  "0002-7863",                                 ← 印刷版 ISSN（必填）
  "eissn": "1520-5126",                                 ← 电子版 ISSN（没有则 ""）
  "abbr":  "J Am Chem Soc",                             ← 常用缩写（检索时用）
  "IF":    14.7,                                        ← 影响因子（没有则 null）
  "bank":  "SCIE",                                      ← WoS 收录类型（见下表）
  "jcr":   1,                                           ← JCR 分区 1~4（没有则 null）
  "cas":   1,                                           ← 中科院分区 1~4（没有则 null）
  "top":   true,                                        ← 是否中科院 Top 期刊
  "ei":    false,                                       ← 是否 EI 收录
  "cscd":  null,                                        ← CSCD: 1=核心库 2=扩展库 null=未收录
  "pku":   false,                                       ← 是否北大核心
  "sos":   null                                         ← 预警期刊（见下表）
}
```

### bank 可选值

| 值 | 含义 |
|---|---|
| `"SCIE"` | Science Citation Index Expanded |
| `"SSCI"` | Social Sciences Citation Index |
| `"ESCI"` | Emerging Sources Citation Index |
| `"AHCI"` | Arts & Humanities Citation Index |
| `"SCIE/SSCI"` | 同时被 SCIE 和 SSCI 收录 |
| `"SCIE/SSCI/AHCI"` | 三库同收 |
| `"SCIE/AHCI"` | SCIE + AHCI |
| `"SSCI/AHCI"` | SSCI + AHCI |
| `null` | 未被 WoS 收录 |

### sos 预警格式

```json
"sos": { "24": 2 }
```

- key 是年份后两位（"24" = 2024年）
- value 是预警等级：

| 值 | 含义 |
|---|---|
| 1 | 高预警 |
| 2 | 中预警 |
| 3 | 低预警 |
| 4 | 引用操纵 |
| 5 | 引用操纵/论文工厂 |
| 6 | 论文工厂 |
| 7 | 论文工厂/CN作者占比畸高 |
| 8 | CN作者占比畸高 |

多年预警示例：`"sos": {"23": 3, "24": 2}` （2023年低预警，2024年中预警）

---

## 构建步骤

```bash
# 在 data-source/ 目录下
python build-data.py
```

输出 `odata.json`，将其上传到你的 git 仓库（建议用 gitee 保证国内访问速度）。

---

## 配置数据源 URL

上传后，打开 `src/data/journal-loader.ts`，修改第 14~17 行：

```ts
const DATA_SOURCES = [
  'https://gitee.com/你的用户名/你的仓库/raw/main/odata.json',   // 主源（国内快）
  'https://raw.githubusercontent.com/你的用户名/你的仓库/main/odata.json', // 备用
];
```

---

## 更新数据版本

每次大规模更新期刊数据后，修改 `src/data/journal-loader.ts` 第 11 行的版本号（格式随意，只要变化就会触发用户端重新拉取）：

```ts
const DATA_VERSION = 'jt_240622';  // 改成新的版本号，如 'jt_250101'
```
