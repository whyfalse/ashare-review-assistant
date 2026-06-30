# Runtime Data Convention

本目录是A股投资技能共用的运行时用户数据存放位置。

## 文件清单

| 数据文件 | 从模板初始化 | 读取方 | 写入方 |
|---|---|---|---|
| `positions.json` | `../templates/positions_template.json` | intraday-review, evening-review, morning-brief, weekly-review | 用户(手动) |
| `watchlist.json` | `../templates/watchlist_template.json` | intraday-review, evening-review, morning-brief, weekly-review | 用户(手动) |
| `macro_context.json` | `../templates/macro_context_template.json` | 所有技能 | macro-context（唯一写入者） |
| `macro_updates_queue.json` | 无(运行时自动创建) | macro-context | intraday-review, evening-review, morning-brief（追加建议） |
| `sector_watchlist.json` | `../templates/sector_watchlist_template.json` | weekly-review | 用户(手动) |

## 初始化

将对应模板复制到本目录并填入个人数据:

```bash
cp ../templates/positions_template.json ./positions.json
# 然后编辑 positions.json 填入实际持仓数据
```

## 原则

- `data/` 目录下的文件是个人运行时数据，不应提交到版本控制（建议加入 `.gitignore`）
- `../templates/` 中的模板是结构定义，应保留在版本控制中
- 各技能读取 `data/` 文件，写入规则如下：
  - `ashare-macro-context` 是 `macro_context.json` 的**唯一写入者**（完整维护流程，含消费 `macro_updates_queue.json`）
  - `ashare-intraday-review`、`ashare-evening-review`、`ashare-morning-brief` 只写入 `macro_updates_queue.json`（追加 Tier 3 更新建议），不直接修改 `macro_context.json`
  - `ashare-weekly-review` 不写入 `macro_context.json`，宏观维护完全委托给 `ashare-macro-context`
- `ashare-intraday-review` 只读取 `positions.json`/`watchlist.json` 做盘中核对，并对短期机会做当日发掘式输出。
