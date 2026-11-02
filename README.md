# BetterChat

> 让 AstrBot 在私聊里**等你把话说完**再回答，在群聊里**自己判断要不要插话**。

BetterChat 是一个 AstrBot 插件，只做两件事，但都把细节做透了：

| 场景 | 原生 AstrBot | BetterChat |
|---|---|---|
| 私聊连发 3 条 | 回 3 次（或按框架 chase 逻辑排队） | **合成一条**，只回 1 次 |
| 私聊生成中又发消息 | 排队等旧的说完 | **立刻中止旧生成**，丢弃旧回复，只答新的 |
| 群聊没 @ Bot | 完全不理 | **用 Jev 判定**该不该接话，可以主动插话 |
| 群聊 @ 了 Bot 但没必要回 | 一定回 | Jev 过滤掉「叫了但没事」的情况 |

群聊判定使用 [TypeSafe **Jev**](https://docs.typesafe.ai/) —— 一个专门做结构化是非判断的模型，不是聊天模型。它的输出是 0~1 的概率，**要不要说话的策略写在我们代码里**，所以调阈值不需要重新请求。

---

## 功能一：私聊耐心等待

用户说话常常是分几条发的：

```
用户：那个
用户：我想问一下
用户：关于退款的事
```

原生 AstrBot 会对着第一条就开口。BetterChat 的做法是：

1. 第一条到达时不回复，开始计时。
2. 后续消息不断把计时**往后推**（真正的 trailing-edge 防抖）。
3. 安静满一个窗口后，把这一整串**合并成一条输入**交给模型。

合并后模型看到的是完整一句话，回答自然准确。

### 三个边界，不是只有一个

只用一个「静默 2 秒」是不够的 —— 用户一直打字 Bot 就永远不说话。所以有三层：

| 参数 | 作用 |
|---|---|
| `min_quiet_seconds` / `max_quiet_seconds` | 静默窗口的浮动范围 |
| `base_quiet_seconds` + `length_scale_chars` | 文本越长，窗口越长（话说得多 = 可能还没说完） |
| `total_seconds` | **从第一条算起的硬上限**，防止无限打字饿死回复 |

### 打断生成

Bot 正在生成回复时用户又发来一条：

1. 立刻给旧事件打上失效标记；
2. 调用 AstrBot 的 `request_agent_stop_all` 优雅中止生成（保留半截内容并正常写入历史，不会污染上下文）；
3. 旧事件在 `on_llm_request` 和 `on_decorating_result` 两级被拦截，**结果链被清空，一个字都不会发出去**；
4. 新消息重新走合并窗口。

> ⚠️ **流式输出的限制**
> 开启 `provider_settings.streaming_response` 时，AstrBot 会整段跳过 `on_decorating_result`。
> 此时打断只能做到「尽快停止生成」，**已经发出去的片段收不回来**。
> 想要「旧回复完全不出现」的严格语义，请**关闭流式输出**（默认就是关闭的）。

---

## 功能二：群聊 Jev 判定

### 为什么需要它

群聊里原生的规则只有「被 @ 才回」。但真实群聊里：

* 没 @ 你，但有人问了个没人会的问题 —— 你其实应该接；
* @ 了你，但只是随手一提 —— 你不一定该长篇大论。

Jev 用极低成本判断这两件事。

### 上下文：必须送，而且要自己记

**AstrBot 的 `AstrBotMessage` 只有当前这一条消息**（`message` / `message_str` /
`sender` / `timestamp`），没有"最近 N 条群消息"这种东西。所以上下文必须插件自己缓存。

横向对比三家同类插件，做法一致：

| | 上下文来源 | 容量 |
|---|---|---|
| reply_gate | 自己在 `event_message_type(ALL)` 里记 | `deque(maxlen=12)` |
| czyczy23 | 自己的 `pending_messages_cache` | 默认 12 条 |
| jev_router | 读 AstrBot 的**对话历史**（conversations 表） | 可配轮数 |
| **BetterChat** | 自己的 `TranscriptCache` | 30 条 |

`jev_router` 那个是"和 LLM 的对话记录"，不是"群里的聊天流"，对判断该不该插话帮助有限；
我们和另两家一样用内存缓存，零 IO、不依赖 AstrBot 内部表结构。

#### 带不带上下文，差别有多大（实测）

用真实 Jev 实测了两组场景：

| 场景 | 消息 | 带上下文 | 无上下文 |
|---|---|---|---|
| Bot 刚答过同一问题 | 那就是装 aiohttp 吗 | `answered=0.94` → **ignore** ✅ | `answered=0.03` → **respond** ❌ |
| 群里别人已经回答 | 参数是什么呢 | `open_q=0.31` → **ignore** ✅ | `open_q=0.95` → **respond** ❌ |

**没有上下文，两种情况下 Bot 都会重复回复。** 这正是群聊机器人最让人烦的行为。

但也测到了反例 —— 有些场景上下文几乎不起作用：

| 场景 | 带上下文 | 无上下文 |
|---|---|---|
| 「那怎么解决呢」（追问） | open_q=0.96 → respond | open_q=0.92 → respond |
| 「对，就是这样」（省略句） | open_q=0.04 → ignore | open_q=0.03 → ignore |

结论很清楚：**上下文的价值主要在"避免重复回复"，而不是"理解省略句"。**
所以 `context_messages` 不需要开很大 —— 10 条足够覆盖"刚说过什么"，
再往上收益递减，只增加 token 和延迟。

### 判定前的聚合（比同类插件更省）

群消息到达后先安静 **1.5 秒**（可调），期间同一个人连发的多条会合并成
**一次** Jev 判定：

```
用户：那个
用户：我想问一下         ← 这 1.5 秒内
用户：关于退款的事
        ↓
   合并成一次判定（不是三次）
```

- 群里连刷三句只花 **1 次调用**，而不是 3 次
- `open_question` 看到的是合并后的完整语义，不是被打断的半句
- 对未公开的免费额度上限也更友好

| 消息类型 | 是否聚合 |
|---|---|
| 被 @ / 引用 / 唤醒词 | ❌ **立即判定**（用户叫了就该马上响应） |
| 未唤醒的主动插话 | ✅ 聚合 |

> 对比：`astrbot_plugin_reply_gate`、`astrbot_plugin_jev_router`、
> `czyczy23/astrbot_plugin_jev` **都是每条消息判一次**，没有一家在判定前聚合。
> `czyczy23` 虽有"等待窗口"，但那是在**决定回复之后**才开的（用于攒更多消息
> 一次性回复），判定本身早就做完了，而且是 100ms 轮询。
>
> 想在群里连刷时更像人、更省额度，就把窗口调大（如 2~3 秒）；
> 想要最快响应，设为 0 即可恢复"每条各判一次"。

### 工作流程

```
群消息到达
   ↓
① 本地闸门（零延迟，不发请求）
   指令消息 / 自己发的 / 太短 / 超频 / Bot 冷却 / 熔断中  →  直接跳过
   ↓ 通过
② 一次请求问 3 个是非题（并行，成本≈0）
   addressed_to_bot        —— 这话是对着 Bot 说的吗
   open_question           —— 有个还没人回答的问题吗
   already_answered_by_bot —— Bot 是不是刚答过同一件事
   ↓
③ 严格校验（键集合精确匹配、拒绝 bool 冒充数字、拒绝 NaN、范围检查）
   ↓
④ 代码里出结论（三态）
   RESPOND   → 放行，允许主动插话
   IGNORE    → 禁止本轮 LLM
   UNKNOWN   → 按「失败策略」处理
```

策略写死在代码里，所以调阈值不花钱：

```
已经答过        → 不回（防重复）
被 @ 了         → 回
没被 @：
  明显在问 + 有未答问题 → 主动插话
  有人问了没人接        → 主动帮一把
  其余                  → 闭嘴
```

### 自动复读（默认关闭）

群里出现重复消息时，跟着复读一份：

```
用户A：哈哈哈
用户B：哈哈哈
用户C：哈哈哈          ← 达到阈值
Bot：哈哈哈            ← 跟读
```

**几个刻意的设计**：

| 规则 | 原因 |
|---|---|
| 必须**不同的人**发的 | 一个人连刷是刷屏，不是复读 |
| 同一人连发 → 窗口清空 | 同上 |
| 一个内容只跟**一次** | 否则 20 个人复读 Bot 跟 20 次 |
| 直接转发原消息，**不经 LLM** | 复读的灵魂就是原样再发；让模型生成反而不像 |
| 命中但决定不跟 → 也吞掉消息 | 已确认是复读场景，再触发 LLM 回复会很怪 |

阈值默认 文字 4 / 图片 3 / 表情 2（表情最容易撞，所以最低）。跟读概率默认 80%，
留一点不跟的余地，避免显得有求必应。

### 防冷场（默认关闭）

群安静太久时主动开口暖场：

```
群安静 20 分钟
    ↓
本地闸门：免打扰 / 冷却 / 今日次数 / 群活跃度
    ↓
Jev 判定：现在开口合适吗
    ↓
用官方 CronMessageEvent 注入事件队列 → 主 LLM 生成
```

**为什么必须走官方管线**：直接用 `provider.text_chat` 会绕过整个框架，
人设、长期记忆、工具、发送钩子全部失效，说出来的话会完全不像这个 Bot。
`CronMessageEvent` 是 AstrBot 官方提供的合成事件
（`astrbot_core.cron.events`），走 `EventBus` → 完整 `PipelineScheduler`，
效果等价于"有人在说话"，只是说话者是定时任务。

**四道保护**（都可配）：

| 保护 | 默认 | 作用 |
|---|---|---|
| 免打扰时段 | `23-7` | 深夜不主动，支持跨午夜 |
| 需要历史活跃 | 开 | 刚拉进群的 Bot 不会自言自语 |
| 每日上限 | 3 次 | 防止反复破冰 |
| Bot 冷却 | 30 分钟 | Bot 刚说过话就不主动 |

**判定策略**（实测调过）：三个信号组合，而不是只看一个。实测「有人提了个没人
接的问题」时 `should_speak` 只给 0.56，但 `has_topic` 高达 0.88 ——

```
气氛不合适（mood_fit < 0.5）      → 一律不说（吵架/严肃话题）
有话可接（topic≥0.6）且气氛允许    → 说
should_speak ≥ 0.8（强烈建议）     → 说
其余                              → 沉默
```

实测四种场景：

| 场景 | ss | topic | mood | 结论 |
|---|---|---|---|---|
| 有未答问题 | 0.58 | **0.88** | 0.85 | 开口 ✅ |
| 正常收尾 | 0.07 | 0.06 | 0.82 | 沉默 ✅ |
| 在吵架 | 0.20 | 0.73 | **0.05** | 沉默 ✅ |

### 送给 Jev 的是什么

Jev **没有 prompt / system 字段** —— 它只接受三样东西：

| 字段 | 放什么 | 不放什么 |
|---|---|---|
| `state` | **事实**：群名、Bot 名字、谁在说话、最近消息 | 规则、命令、"你应该…" |
| `instructions` | **问什么**，以及看哪一部分（`` `latest_message` ``） | — |
| `criteria` | **边界定义**：什么算"是"，什么算"否" | — |

官方文档明确要求 `state` 只放与当前问题相关的内容，规则写进
`instructions` / `criteria`，否则会造成 *distractions* 和 *context rot*。
本插件按这条执行：

```json
// state：全是事实
{
  "group": "测试群",
  "bot": { "name": "HexVergeBot", "mentioned_in_latest": true },
  "latest_sender": "秉鑫",
  "latest_message": "有人知道这个报错怎么解决吗",
  "recent_messages": ["刚才那个配置我改了"],
  "recent_bot_messages": ["在的，怎么了？"]
}
```

> **`bot.name` 是必需的。** 不告诉 Jev Bot 叫什么，判断"这话是不是对 Bot
> 说的"就只能靠猜 `@后面那串` 是不是自己。
> `mentioned_in_latest` 则是平台层面**已经确定**的事实（被 @ / 被引用 /
> 唤醒词），比让模型从文本里推断可靠得多。

问题用官方的**结构化写法**（`instructions` 和 `criteria` 的值都支持
字符串/对象/数组），把"看哪部分"和"按什么标准看"拆成独立字段：

```json
{
  "type": "noul",
  "instructions": {
    "question": "Is `latest_message` aimed at the assistant (the bot)?",
    "compare": ["`latest_message`", "`bot`", "`recent_bot_messages`"],
    "focus": "Look for the bot being named or addressed..."
  },
  "criteria": {
    "true":  { "what": "The message targets the bot itself", "examples": ["..."] },
    "false": { "what": "Targets other people", "not_for": "..." }
  }
}
```

三个问题的分工（对应官方"拆成原子问题"的要求）：

| 问题 | 回答什么 | 为什么单独问 |
|---|---|---|
| `addressed_to_bot` | 这话是冲着 Bot 来的吗 | 决定"该不该接话" |
| `open_question` | 有没有还没人接的问题 | 决定"能不能主动插话" |
| `already_answered_by_bot` | 刚是不是已经答过 | 防止重复回复 |

拆开而不是问一个"该不该回复"，是因为后者把三个判断藏在一个数字背后 ——
既没法单独调阈值，也看不出是哪一步否掉的。

### 三个渠道

| | TypeSafe 官方 | Cloudflare Workers AI | OpenCode |
|---|---|---|---|
| 端点 | `api.typesafe.ai/v1/systemone` | `.../accounts/{id}/ai/run` | `opencode.ai/inference/systemone/v1/systemone` |
| 请求形状 | `{model, state, questions}` | `{model, input:{state, questions}}` | `{model, state, questions}` |
| 需要 | API Key | 账号 ID + Token | Console key（`oc_sk_` 开头） |
| 免费可用 | ❌（但很便宜） | ❌ 免费额度不覆盖第三方模型 | ✅ **`jev-1.13-free` 免费** |

#### OpenCode 免费 jev —— 可以用，但必须带齐三个东西

`jev-1.13-free` **是可以从插件调用的**（实测返回 200 且 `usage.cost == 0`），
但少任何一个条件都会失败，而且失败信息很有迷惑性：

| 必需项 | 缺了会怎样 |
|---|---|
| `Authorization: Bearer oc_sk_…` | `401` |
| `x-opencode-session: <uuid>` | `400 MissingSessionID` ← 不是 401，容易误判成权限问题 |
| 正常的 `User-Agent` | `403 error code: 1010` ← Cloudflare 拦 `Python-urllib`/`aiohttp` 默认 UA，看着像 key 无效 |

**本插件已自动处理这三项**，你只需要在面板填 `oc_api_key`。

实测判断质量示例（免费模型，cost 0）：

| 消息 | addressed | open_q | 结论 |
|---|---|---|---|
| 有人知道这个报错怎么解决吗 | 0.05 | **0.97** | respond（主动帮） |
| HexVergeBot 帮我看下这个 | **0.96** | 0.81 | respond（被叫到） |
| 哈哈哈笑死 | 0.03 | 0.02 | ignore（不打扰） |

> ⚠️ **限时免费 + 未公开用量上限。** 用尽后返回 `429 FreeUsageLimitError`，
> OpenCode 没有公布上限大小和重置时间。用尽时可切付费 `jev-1.13`
> （需账户余额），或改用 TypeSafe 官方渠道。

#### OpenCode Go 里没有 Jev

Go 订阅（$10/月）虽然允许第三方客户端调用，但**它的模型清单里没有 Jev**。
实测：`opencode.ai/zen/v1/models` 返回 78 个模型，无一含 jev；带 session 头打
Go 路由返回 `Model is unavailable`。所以不为 Go 单独做渠道。

### 降级：任何失败都不影响可用性

| 情况 | 结果 |
|---|---|
| 没配 Key | 群聊闸门**自我禁用**，退回原生行为（只有 @ 才回），私聊合并照常工作 |
| 超时 / 网络错误 / 坏 JSON | 按「失败策略」，默认不回复 |
| 返回内容不符合契约 | 同上（宁可不说，也不乱说） |
| 连续失败 N 次 | 熔断一段时间，期间零请求 |
| **被 @ 的消息** | **永远放行** —— 用户明确叫了 Bot，不能因为判定服务挂了就不理人 |

---

## 安装

把本仓库放到 AstrBot 的插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/AS214933/astrbot-betterchat.git
```

然后在 WebUI 的「插件」页重载插件。或者在**插件市场**添加仓库地址：
`https://github.com/AS214933/astrbot-betterchat`

**要求**：AstrBot **≥ 4.28.0**。更早的版本在等待 LLM 的钩子里不响应 `stop_event()`，打断会失效。

---

## 配置

**全部配置都在管理面板里改**：「插件」→ BetterChat → 配置。分成五组：基础 / 私聊合并 / 打断 / 群聊闸门 / Jev 渠道。

> 💡 **保存后自动生效。** AstrBot 在保存插件配置时会自动热重载插件，不需要重启。
> 但也因此：**保存的那一刻，正在等待合并的会话会被取消**（那条消息不会得到回复）。这是热重载的固有代价，改配置时留意一下即可。

### 常用参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `private.base_quiet_seconds` | 2.0 | 安静多久算说完 |
| `private.total_seconds` | 8.0 | 从第一条算起的硬上限 |
| `interrupt.mode` | graceful | 打断方式 |
| `group.judge_all_messages` | true | true=可主动插话；false=只判被 @ 的 |
| `group.help_unaddressed` | true | 有人提问没人接时，观望后再帮 |
| `group.help_unaddressed_delay_seconds` | 90 | 观望多久没人接才帮 |
| `group.max_replies_per_hour` | 8 | 每群每小时最多主动回复 |
| `group.fail_policy` | closed | Jev 挂了是否放行 |
| `jev.channel` | official | 官方 / Cloudflare |
| `jev.reply_threshold` | 0.7 | 调高更保守，调低更主动 |

---

## 指令

```
/betterchat status        查看当前渠道、端点、模型、熔断状态、本群限频
/betterchat gate <文本>   干跑一次判定：打印 3 个概率 + 阈值敏感性表（不会回复）
/betterchat reset         清空本会话限频计数（管理员）
```

`/betterchat gate` 是调参利器 —— 你可以拿真实群聊里的一句话试，直接看到「阈值调到多少会改变结论」：

```
BetterChat 群聊判定（干跑，不会回复）
───────────────
文本：有人知道这个报错怎么解决吗
addressed_to_bot       = 0.120
open_question          = 0.940
already_answered_by_bot= 0.030

阈值敏感性：
  ≥0.5 → respond（unanswered_question）
  ≥0.6 → respond（unanswered_question）
  ≥0.7 → respond（unanswered_question） ←当前
  ≥0.8 → respond（unanswered_question）
  ≥0.9 → ignore（not_addressed）
```

---

## 设计说明

### 为什么不用 `session_waiter`

AstrBot 提供了 `@session_waiter` 做会话控制，看起来很适合防抖。但它被设计成「插件接管对话」，被捕获的消息**不会再经过任何其他插件，也不会走原生 LLM 流程** —— 意味着人设、记忆、工具、上下文全部丢失。

BetterChat 选择在 `on_waiting_llm_request` 阶段介入并**放行原生流程**，只改写 `event.message_str`。这样：

* ✅ 人设、人格设定生效
* ✅ 长期记忆、会话历史正常
* ✅ 工具调用照常
* ✅ 其他插件的行为不受影响

### 为什么 `should_call_llm` 看着像写反了

AstrBot 的这个 API 语义是**反的**：

```python
# 源码：astr_message_event.py
def should_call_llm(self, call_llm: bool) -> None:
    """是否在此消息事件中**禁止**默认的 LLM 请求。"""
    self.call_llm = call_llm

# 源码：process_stage/stage.py —— 注意那个 not
if not event._has_send_oper and event.is_at_or_wake_command and not event.call_llm:
```

也就是说 `should_call_llm(True)` = **禁止**调用。为了避免在每个调用点重复踩坑，插件内部用两个语义明确的小函数包了一层：`_take_over_turn()` 和 `_let_astrbot_handle()`。

### 群聊为什么能主动插话

未唤醒的群消息默认过不了 `ProcessStage` 的放行条件（`is_at_or_wake_command` 为假）。插件的群聊 handler 命中所有群消息，判定要回复时主动把该标志置真，从而让原生流程接手。

### 并发安全

* 每条消息的登记是**同步**完成的（内部无 `await`），因此 ticket 顺序天然等于到达顺序；
* 等待用 `asyncio.Event` + 版本号，不轮询；
* `wake.clear()` 在复查版本号**之前**执行，避免经典的 lost-wakeup；
* 结算时用锁保证只有最新那条能取走合并文本。

### 热重载安全

面板每保存一次配置都会 `terminate()` 再重建。所以 `terminate()` 必须做到：唤醒全部等待者、取消全部后台任务、关闭 HTTP 连接、吞掉所有异常。否则改几次配置后就会开始行为错乱。

---

## 开发

```bash
python -m pytest tests/ -q
```

110 个测试，**全离线**（不需要 AstrBot、不需要网络、不需要 API Key），约 0.4 秒跑完。

| 测试文件 | 覆盖 |
|---|---|
| `test_merge_window.py` | 窗口计时、ticket 仲裁、超限开新轮、硬上限、剪枝 |
| `test_jev_parity.py` | 两个渠道的请求形状、响应解包、归一化一致性、熔断 |
| `test_jev_validate.py` | 逐条拒绝规则（bool 冒充数字 / NaN / 键不匹配 / 越界） |
| `test_gate_policy.py` | 本地闸门短路顺序、概率→动作映射、失败策略 |
| `test_wiring.py` | 用 stub 的 astrbot 加载 `main.py`，验证钩子签名与关键语义 |

代码分层：

```
main.py             AstrBot 接线层（唯一重度依赖 astrbot 的文件）
betterchat_core.py  纯逻辑：窗口计时、闸门、决策   ← 可独立单测
betterchat_jev.py   Jev 客户端：双渠道 + 校验 + 熔断
betterchat_state.py 运行态与限频持久化
```

纯逻辑层不 import astrbot，所以能在没有框架的环境里完整测试。

---

## 更新日志

完整版本历史见 [CHANGELOG.md](CHANGELOG.md)。

几个值得注意的**会静默失效**的修复（不报错、但功能完全不生效），
如果你用的是旧版本，建议对照确认：

| 版本 | 问题 | 症状 |
|---|---|---|
| 1.0.1 | Cloudflare 渠道端点写错 | 群聊全部 `400 code 7000 No route for that URI` |
| 1.0.2 | `ComponentType` 是 `str` 的子类，`type(x) is str` 恒为假 | 私聊每条消息都立刻回复，合并完全没生效 |
| 1.3.0 | 并发下没有事件负责结算 | 群里刷屏时 Bot 突然不说话 |
| 1.3.1 | 读了 `AstrBotMessage` 上不存在的属性 | 群聊上下文一直是空的，判定在盲猜 |

## 已知限制

* **流式输出下打断不彻底** —— 见上文「打断生成」的警告。
* **群聊上下文依赖 AstrBot 的群历史配置**。主动插话时如果拿不到群历史，Bot 只看到触发的那条消息，判断会偏保守。建议开启 `provider_lmt_settings.group_message_history_enable` 让群聊历史可用。
* **只合并纯文本**。带图片、文件、语音、合并转发的消息直接放行走原生流程 —— 重建这类消息链容易出错，不值得冒险。
* **保存配置会取消进行中的合并窗口**（热重载的固有代价）。

---

## 许可与致谢

本项目使用 **MIT** 许可，见 [LICENSE](LICENSE)。

设计过程中参考了以下项目（均为 MIT 许可）的思路，特此致谢：

* [AstrBotDevs/astrbot_plugin_reply_gate](https://github.com/AstrBotDevs/astrbot_plugin_reply_gate) —— 群聊拦截钩子的选择（`on_waiting_llm_request` 在会话锁之前，拒绝零成本）、严格答案校验、纯逻辑与接线分离的架构，以及 `/gate test` 干跑指令的思路。
* [ahuai114514/astrbot_plugin_message_merger](https://github.com/ahuai114514/astrbot_plugin_message_merger) —— 用 `asyncio.current_task()` 绑定管线任务、以及在多个阶段拦截失效结果的防御性写法。
* [maolbsMd/astrbot_plugin_jev_router](https://github.com/maolbsMd/astrbot_plugin_jev_router) —— Jev 请求的构造方式、熔断与降级策略、URL 加固。

AstrBot 本身使用 AGPL-3.0；本插件通过公开插件 API 与其交互，不包含其源代码。

Jev 的请求/响应契约依据 [TypeSafe 官方文档](https://docs.typesafe.ai/) 与 [Cloudflare Workers AI 文档](https://developers.cloudflare.com/ai/models/typesafe/jev/) 实现。
