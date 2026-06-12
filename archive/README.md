# archive/

旧版本、废弃用例、被替代的业务规则版本归档处。

## 何时入档

- `_kb/projects/<product>/rules.md` 某章节被本次需求**完全替换**时，旧版章节移到这里
- 历史工单中 AI 写错的典型反例（已被改正版本替代）
- 项目结构调整时被废弃的旧文件（不直接删除，存档以备追溯）

## 目录约定

```
archive/
├── rules/
│   └── <product>/
│       └── <YYYY-MM-DD>_<section>_<reason>.md
├── tickets/
│   └── <product>/<ticket>/                 整个工单归档
└── snapshots/
    └── <YYYY-MM-DD>_<descriptor>/          仓库整体快照（重大重构前）
```

## 何时**不要**入档

- 当前在用的内容（直接放 `_kb/`）
- 凭证、敏感数据（直接删，不要进 Git 历史）
- 临时调试产物（用 `tmp/` 并 gitignore）

## 检索

`archive/` 内容也参与 `/qa:kb-search`（按需）。

```
/qa:kb-search "旧拣货优先级"
```

AI 命中 archive/ 时会明确标注 `[来源: archive/...]`，提醒"这是历史版本"。
