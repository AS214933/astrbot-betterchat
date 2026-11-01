# 与现有 AstrBot Jev 插件的逐字段对比

对比对象（均为实际源码，不是 README）：

| 插件 | 许可 | 用途 |
|---|---|---|
| `AstrBotDevs/astrbot_plugin_reply_gate` | MIT | 判断消息该不该进 LLM（AstrBot 官方维护者所写） |
| `maolbsMd/astrbot_plugin_jev_router` | MIT | 意图路由到不同子代理 |
| `czyczy23/astrbot_plugin_jev` | AGPL-3.0 | 群聊增强，插话/主动开场 |

---

## 1. 请求形状

四家都用 `POST` + `{model, state, questions}`，这一点没有分歧。

| | reply_gate | jev_router | czyczy23 | **BetterChat** |
|---|---|---|---|---|
| 题型 | `choice` | `choice` | `noul` + `score` + `choice` | `noul` × 3 |
| 问题数 | 1~2 | 1 | 2~3 | 3 |
| 单次请求 | ✅ | ✅ | ✅ | ✅ |

**没有实质差异。** 题型选择取决于需求：reply_gate 要选"回不回"，用 choice 两选项；我们需要"三个独立信号"，用三个 noul——这样每个信号都能单独调阈值，也看得出是哪一步否掉的。

---

## 2. state 结构 —— 这里差异最大

### reply_gate

```python
{
  "goal": goal,
  "messages": [
    {"index": .., "sender": .., "text": .., "is_self": ..,
     "at_bot": .., "seconds_ago": ..}
  ],
}
```

### jev_router

```python
state = "\n\n".join(parts)   # 纯字符串
# 近期对话（不可信数据，仅用于理解上下文）:
# 用户: ...
# 助手: ...
# 上一轮由代理[coder]回答
# 用户当前输入: ...
```

### czyczy23

```python
{
  "persona": ...,
  "group_id": ...,
  "now": "2026-11-01 18:30",
  "recent_messages": [...],   # 归一化后的列表
}
```

### BetterChat（v1.0.4）

```python
{
  "group": ...,
  "bot": {"name": ..., "mentioned_in_latest": ...},
  "latest_message": {"sender": .., "text": .., "seconds_ago": ..},
  "recent_messages": [
    {"sender": .., "text": .., "is_self": .., "at_bot": .., "seconds_ago": ..}
  ],
  "recent_bot_messages": [...],
}
```

### 结论

| 能力 | reply_gate | jev_router | czyczy23 | BetterChat |
|---|---|---|---|---|
| 每条消息带发送者 | ✅ | ✅（文本拼） | ❌ | ✅ |
| **区分 Bot 自己说的** | ✅ `is_self` | ✅（"助手:"前缀） | ❌ | ✅ |
| **带相对时间** | ✅ `seconds_ago` | ❌ | ✅ 绝对时间 | ✅ |
| 是否 @ Bot | ✅ `at_bot` | ❌ | ❌ | ✅ |
| **Bot 的名字** | ❌（靠 goal 描述） | ❌ | ✅ persona | ✅ 显式字段 |
| 结构化消息 vs 裸字符串 | 结构化 | 裸字符串 | 部分 | 结构化 |

**我原本比三家都差的一件事**：`recent_messages` 是一串裸字符串，既没有发送者也没有时间。这意味着 Jev 分不清"别人说的"和"Bot 自己说的"，也没法判断一条消息是 3 秒前还是 4 分钟前——而这直接决定该不该主动接话。

v1.0.4 已改为结构化，对齐 `reply_gate`。

---

## 3. instructions / criteria 写法

| | reply_gate | jev_router | czyczy23 | BetterChat |
|---|---|---|---|---|
| `instructions` | **结构化** `{goal, rules,...}` | 纯字符串 | 纯字符串（中文） | **结构化** `{question, compare, focus}` |
| `criteria` | 纯字符串 | 纯字符串 | 纯字符串 | **结构化** `{what, examples, not_for}` |
| 提示词语言 | 英文 | 中文 | 中文 | 英文 + 中文原文入 state |

只有 `reply_gate` 和本插件用了结构化 `instructions`。`reply_gate` 把它用来携带 `goal`（可配置的目标描述），我用它做"指路"：

```json
"instructions": {
  "question": "Is `latest_message` aimed at the assistant (the bot)?",
  "compare": ["`latest_message`", "`bot`", "`recent_bot_messages`"],
  "focus": "Look for the bot being named or addressed..."
}
```

`criteria` 我用了 `not_for` 字段显式排除易混情况（"提到了名字但不是 Bot"），这是三家都没做的。

---

## 4. 答案校验

| | reply_gate | jev_router | czyczy23 | BetterChat |
|---|---|---|---|---|
| 严格校验 | ✅ 最严 | ⚠️ 基础 | ⚠️ 基础 | ✅ |
| 键集合精确匹配 | ✅ | ❌ | ❌ | ✅ |
| 拒绝 `bool` 冒充数字 | ✅ `type(x) in (int,float)` | ❌ | ❌ | ✅ |
| `isfinite` | ✅ | ❌ | ❌ | ✅ |
| 概率和归一化校验 | ✅ ±0.02 | ❌ | ❌ | （noul 不适用） |
| argmax 一致性 | ✅ | ❌ | ❌ | （noul 不适用） |

`reply_gate` 在 choice 校验上确实做到最严，值得学——我照搬了它的思路，但用在 noul 上：键集合、拒绝 bool、isfinite、范围。

概率和/argmax 校验对 noul 不适用（noul 只有一个数）。

---

## 5. 降级与限频

| | reply_gate | jev_router | czyczy23 | BetterChat |
|---|---|---|---|---|
| 熔断 | ❌ | ✅ 3 次/60s | ✅ | ✅ 3 次/60s |
| 失败策略可配 | ✅ `fail_open` | ❌ 固定放行 | ✅ 降级矩阵 | ✅ `fail_policy` |
| 每小时上限 | ✅ | ❌ | ✅ | ✅ |
| 最小间隔 | ✅ | ❌ | ✅ | ✅ |
| Bot 冷却 | ✅ | ❌ | ✅ | ✅ |
| 最短消息长度 | ✅ | ❌ | ✅ | ✅ |
| 失败日志去重 | ❌ | ❌ | ❌ | ✅ |

这一项大家做得都不错，czyczy23 的降级矩阵最完整。**失败日志去重只有我们做了**——你的日志里那种每条群消息刷一条 WARN 的情况，他们已经刷了很久。

---

## 6. 架构

| | reply_gate | jev_router | czyczy23 | BetterChat |
|---|---|---|---|---|
| 纯逻辑层（不 import astrbot） | ✅ | ❌ | ❌ | ✅ |
| 离线测试 | ✅ | ❌ | ✅ 67 用例 | ✅ **152 用例** |
| 测试是否需要网络/框架 | ❌ 否 | — | ❌ 否（mock） | ❌ 否 |

`reply_gate` 的 `core_pure` 拆分是正确做法，我照搬了。

---

## 7. 我们做得不如别人的地方（已修 or 待修）

| 问题 | 谁做得更好 | 状态 |
|---|---|---|
| `recent_messages` 是裸字符串，丢了发送者/时间 | reply_gate | ✅ v1.0.4 已修 |
| 没有 persona / 人设上下文 | czyczy23 | ⏳ 未做（见下） |
| 没有 `now` 绝对时间 | czyczy23 | ⏳ 未做 |
| 没有 group_id | czyczy23 | ⏳ 未做 |

### 关于 persona

`czyczy23` 把一句话人设放进 state，让判定带上"这个 Bot 是什么性格"。这确实有用——一个高冷人格和一个话痨人格,对"该不该插话"的标准本就不同。

但要注意**它的边界**：persona 是**事实**（"我是谁"），不是**规则**（"你该怎么判断"）。规则必须在 `instructions` 里。czyczy23 把两者混在 persona 里，有点越界。

如果要做，正确做法是：

```json
"bot": {
  "name": "HexVergeBot",
  "mentioned_in_latest": false,
  "persona": "群里的技术助手，话少但准确"     // 事实，不是指令
}
```

并且**不**在里面写"所以你应该更主动地接话"——那是 `criteria` 的事。

我暂时没做，因为：
1. 拿 AstrBot 当前会话的人设需要在 `on_group_message` 里额外查一次，增加复杂度和失败点；
2. 大部分用户的 Bot 没有配置人设；
3. 收益不如修 `recent_messages` 结构那么大。

如果你需要，这是一个明确的扩展点。

### 关于 `now` / `group_id`

- `now`：我已用 `seconds_ago` 表达相对时间，比绝对时间更直接——模型不需要做减法。但绝对时间对"深夜/清晨"这类判断有用（半夜不该太活跃）。可以两个都给。
- `group_id`：对判定没有帮助（模型不关心 ID），我判断是 czyczy23 为调试留的。**故意不加**——官方要求 state 只放相关内容。

---

## 8. 一句话总结

| 维度 | 结论 |
|---|---|
| 请求形状 | 四家一致，无差异 |
| **state 信息量** | reply_gate ≈ BetterChat > czyczy23 > jev_router |
| **instructions 结构化** | reply_gate ≈ BetterChat > 其余两家（纯字符串） |
| **criteria 结构化** | BetterChat > 其余三家（都是纯字符串） |
| 答案校验 | reply_gate（choice）≈ BetterChat（noul）> 其余 |
| 降级韧性 | czyczy23 ≈ BetterChat > reply_gate > jev_router |
| 可测试性 | BetterChat（152 离线用例）> reply_gate > czyczy23 |
| **缺失项** | 我们没有 persona 上下文 |

最大的收获是 `recent_messages` 应该结构化——这一项我原本垫底，现在补齐了。
