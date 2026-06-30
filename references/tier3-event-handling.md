# Tier 3 事件驱动变量处理协议

## 定义

Tier 3 事件驱动变量属于宏观环境记忆中的"发生时即时更新"层，不设固定复核周期，需标注具体日期。

典型例子：央行议息决议、重要会议结论、地缘政治突发事件、关税/出口管制变动、重大人事变动。

## 核心原则

- **单一写入者**：`data/macro_context.json` 的唯一写入者是 `ashare-macro-context`（以及周复盘时委托调用其完整维护流程）。日常复盘/推送技能不直接写入该文件。
- **建议队列**：日常技能发现 Tier 3 级事件时，产出更新建议追加到 `data/macro_updates_queue.json`。
- **自动接力（时效性）**：复盘/推送技能产出队列建议后，由编排器（`review-orchestrator` agent）在同一回合内自动接力调用 `ashare-macro-context` 的【队列消费模式】消费队列，无需用户手动触发。复盘技能本身仍不直接写入 `macro_context.json`，单一写入者原则不变。

## 各技能的处理协议

### 日常复盘/推送技能 (intraday-review, evening-review, morning-brief)

当发现 Tier 3 级别的新事件时：

1. **产出更新建议**，追加到 `data/macro_updates_queue.json`（队列文件格式见下方），不直接写入 `macro_context.json`
2. 判断新信息归属的类别代码（参照 macro-context 的 A-M 框架）
3. 建议类型：
   - `update_existing`：匹配已有条目，建议覆盖式修订 `summary`
   - `new_candidate`：全新事实且现有条目无法覆盖，建议记入 `candidates`
   - `uncertain`：无法确定归属类别，标记待归类
4. **仅作事实记录，不据此下操作结论**
5. 技能结束时输出标准化通知（见下方）

### 队列文件格式 (`data/macro_updates_queue.json`)

```json
{
  "queue": [
    {
      "id": "<source_skill>-<ISO timestamp>",
      "source_skill": "ashare-morning-brief",
      "source_date": "2026-06-25",
      "action": "update_existing | new_candidate | uncertain",
      "category": "I_国际形势",
      "dimension": "中美关系-关税与贸易",
      "summary": "客观事实陈述，不含买卖判断",
      "rationale": "为什么认为是Tier 3级别、为什么现有条目无法覆盖（如适用）",
      "created_at": "2026-06-25T08:15:00"
    }
  ]
}
```

### 标准化通知文本

各日常技能在输出末尾附上（供编排器判断是否接力消费队列）：

> **宏观更新队列**：本次发现 X 条宏观更新建议，已追加至 `data/macro_updates_queue.json`。（由编排器自动接力调用 `ashare-macro-context` 队列消费模式完成写入。）

若本次未发现 Tier 3 事件：

> **宏观更新队列**：本次未发现需要更新宏观记忆的新事件。

> 注：若复盘技能被单独直接调用（未经 `review-orchestrator` 编排器），则不会自动接力，此时需用户手动触发 `ashare-macro-context` 消费队列。

### 周复盘 (weekly-review)

不再直接执行宏观记忆维护。改为：
1. 检查 `data/macro_updates_queue.json` 是否仍有未消费的队列项
2. 如有积压，提示用户先触发 `ashare-macro-context` 消费队列
3. 周复盘本身专注于持仓/自选股/板块/未结事项的复盘

### 宏观背景维护 (macro-context)

本skill有两种运行模式（详见其 SKILL.md「两种运行模式」）：

**队列消费模式 (quick-consume)** —— 由编排器在复盘后自动接力调用，目标是即时落库、保证时效性：
1. 读取 `data/macro_updates_queue.json`
2. 逐条评估建议：确认事实 → 判断归属 → 执行写入（`update_existing` 更新已有条目 / `new_candidate` 新增 candidate）或标记为 `rejected`
3. 清空已处理的队列项；`uncertain` 项不强行归类，保留在队列中留给完整维护处理
4. **不做 candidate 转正、不跑全量 tier 复核**——这些属于完整维护模式

**完整维护模式 (full-maintenance)** —— 用户定期维护或周复盘委托时触发，执行全部 8 步工作流：在读现状之后、逐项核实之前同样包含上述"消费更新队列"步骤，并额外负责全量 tier 复核、到期项检查、candidate 转正/淘汰等框架级决策。

## 硬边界

Tier 3 事件在任何复盘/推送技能中 **只能用于"解释现象"和"产出更新建议"**，严禁作为当日/当周买卖操作的直接依据。宏观记忆的更新属于事实记录，不构成操作建议。
