# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)，
更新日志格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

> **为什么会有这么多 1.0.x**：1.0.0 发布后陆续发现了几个**会静默失效**的问题
> （不报错、但功能完全不生效）。每个修复都补了针对性的回归测试，
> 并在本文件中记录了根因，方便你在遇到类似症状时判断是否已经修掉。

---

## [未发布]

暂无。

---

## [1.4.0] - 2026-11-02

### 新增

- **自动复读**（默认关闭）：群里出现重复消息时跟着复读一份。
  - 判定条件：连续 N 条**指纹相同**、且**来自不同的人**。
  - 阈值默认 文字 4 / 图片 3 / 表情 2。
  - 直接转发原消息链，**不经过 LLM** —— 复读的本质是原样再发一遍。
  - 一个内容只跟读一次；跟读概率默认 80%。
- **防冷场**（默认关闭）：群安静 20 分钟后主动开口暖场。
  - 走官方 `CronMessageEvent` 注入事件队列，人设/记忆/工具全程生效。
  - 四道保护：免打扰时段（`23-7`，支持跨午夜）、需要历史活跃、
    每日 3 次上限、Bot 发言后冷却 30 分钟。
  - 判定使用新的一组 Jev 问题（`should_speak` / `has_topic` / `mood_fit`）。
- 面板新增「自动复读」「防冷场」两个配置分组，共 17 项。

### 修复

- **复读检测误判**：原判据 `len(set(senders)) == threshold` 在 `[b, a, b]`
  这种**交替重复**时会误判成复读（集合大小够，但显然不是三个人在复读）。
  改为要求尾部 N 条**两两不同**。
- **免打扰时段越界静默折值**：原实现用 `% 24` 处理越界，
  `"25-30"` 会被悄悄折成 `"1-6"`，把清晨变成免打扰时段。
  改为越界直接判为非法 —— 配置写错时宁可不限制。

### 变更

- 防冷场的开口判定从「只看 `should_speak >= 0.7`」改为三信号组合。
  实测「有人提了个没人接的问题」时 `should_speak` 仅 0.56，但
  `has_topic` 高达 0.88 —— 只看前者会漏掉最该开口的场景。

---

## [1.3.1] - 2026-11-01

### 修复

- **群聊上下文一直是空的**（关键）。原实现读取
  `event.message_obj.group_messages` / `.history` / `.recent_messages`，
  但 AstrBot 的 `AstrBotMessage` 只有**当前这一条**消息
  （`type` / `self_id` / `message_id` / `group` / `sender` / `message` /
  `message_str` / `raw_message` / `timestamp`），这些属性根本不存在，
  于是每次都静默返回空列表 —— 判定一直在盲猜，**而且不报任何错**。

  改为自行维护 `TranscriptCache`。同类插件也都是这么做的
  （`reply_gate` 用 `deque(maxlen=12)`、`czyczy23` 用 `pending_messages_cache`）。

  实测上下文的价值：

  | 场景 | 带上下文 | 无上下文 |
  |---|---|---|
  | Bot 刚答过同一问题 | `answered=0.94` → 沉默 ✅ | `answered=0.03` → 回复 ❌ |
  | 群里别人已答过 | `open_q=0.31` → 沉默 ✅ | `open_q=0.95` → 回复 ❌ |

  结论：上下文主要在**避免重复回复**，而非理解省略句。所以
  `context_messages` 默认 10 条足够，再往上收益递减。

---

## [1.3.0] - 2026-11-01

### 新增

- **群聊判定前聚合窗口**：群消息到达后先安静 1.5 秒（可配 0~5 秒），
  期间同一个人连发的多条合并成**一次** Jev 判定。
  - 群里连刷三句只花 1 次调用，而不是 3 次。
  - `open_question` 看到的是合并后的完整语义，不是被打断的半句。
  - 被 @ / 引用的消息**不聚合**，立即判定。
  - 窗口设 0 即恢复「每条各判一次」。
- 面板新增「判定前聚合窗口」配置。

### 修复

- **并发下判定丢失**：原设计让 `reserve` 返回「是否并入已有窗口」，
  调用方据此提前返回。但并发时第 0 条会被第 1 条顶成 `SUPERSEDED`，
  而第 1、2 条又都认为自己「已并入」直接退出 —— 结果**没有任何一条
  负责结算**，这次判定凭空消失。表现为群里刷屏时 Bot 突然不说话，
  且不报错。改为所有到达者都进入 `wait`，由最终仍是 owner 的那条收口。

---

## [1.2.0] - 2026-11-01

### 修复

- **推翻 1.1.0 的错误结论：OpenCode 的 `jev-1.13-free` 实际可用。**
  实测从插件调用返回 200 且 `usage.cost == 0`。

  1.1.0 判定「用不了」是因为漏了两个必需条件，而失败信息非常迷惑：

  | 必需项 | 缺了会怎样 |
  |---|---|
  | `Authorization: Bearer oc_sk_…` | `401` |
  | `x-opencode-session: <uuid>` | `400 MissingSessionID`（**不是 401**，极易误判成权限问题） |
  | 正常 `User-Agent` | `403 error code: 1010`（Cloudflare 拦 `Python-urllib` / `aiohttp` 默认 UA，看着像 key 无效） |

  现已全部自动处理，用户只需填 `oc_api_key`。

### 变更

- OpenCode 渠道默认模型改为 `jev-1.13-free`（免费）。
- 错误提示重写：`1010` / `MissingSessionID` / `402` / `429` 各自给出准确解释和出路。

### 说明

- **OpenCode Go 里没有 Jev**。实测 `opencode.ai/zen/v1/models` 返回 78 个
  模型，无一含 jev；带 session 头打 Go 路由返回 `Model is unavailable`。
  因此不为 Go 提供单独渠道。

---

## [1.1.0] - 2026-11-01（**未单独发布**，改动并入 1.2.0）

> 这个版本号在 `metadata.yaml` 里出现过，但由于结论有误，
> 没有单独打 tag 发布，修正后的内容随 1.2.0 一起发布。

### 新增

- 新增 OpenCode 渠道配置（`oc_api_key` / `oc_api_base` / `oc_model`）。

### 当时的错误结论

- 曾判定「OpenCode 免费 jev 只能在 OpenCode 客户端内使用，第三方程序无法调用」。
  **这个结论是错的**，已在 1.2.0 修正。

---

## [1.0.4] - 2026-11-01

### 变更

- **`state` 中的消息改为结构化**。原先是裸字符串数组，既没有发送者也没有时间，
  导致 Jev 分不清「别人说的」和「Bot 自己说的」，也无法判断一条消息是
  3 秒前还是 4 分钟前 —— 而这直接决定该不该主动接话。

  现在每条消息带 `{sender, text, is_self, at_bot, seconds_ago}`，
  `latest_message` 也带发送者与相对时间。没有时间戳时**省略**该字段，
  不编造 0 让模型误以为「刚刚发生」。

### 新增

- 与三家同类插件（`reply_gate` / `jev_router` / `czyczy23`）的逐字段对比文档。

---

## [1.0.3] - 2026-11-01

### 变更

- **按官方规范重构 state 与问题定义**。官方明确要求 `state` 只放事实，
  规则写进 `instructions` / `criteria`，否则会造成 *distractions* 和 *context rot*。
- 问题定义改用官方的**结构化 `EntryType`**：`instructions` 从纯字符串升级为
  `{question, compare|inspect, focus}`，`criteria` 的 `true`/`false` 升级为
  `{what, examples, not_for}`。

### 修复

- **`addressed_to_bot` 只能靠猜**。`state` 里没有 Bot 的身份信息，
  Jev 无从判断「@ 后面那串是不是自己」。补上三项事实
  （`bot.name` / `latest_sender` / `bot.mentioned_in_latest`）。

---

## [1.0.2] - 2026-11-01

### 修复

- **私聊合并完全失效**（关键）。判断「是不是纯文本」时用了 `type(x) is str`，
  但 AstrBot 的类型定义是 `class ComponentType(str, Enum)` —— 它**同时继承**
  `str` 和 `Enum`，因此 `type(x) is str` 恒为 `False`：

  ```python
  >>> type(ComponentType.Plain) is str
  False
  >>> ComponentType.Plain == "Plain"
  True          # ← 等值比较才是对的
  ```

  后果：每条纯文本消息都被判成「非纯文本」，合并逻辑整体被跳过，
  表现为**对每条消息都立刻回复**。改为等值比较。

### 新增

- Jev 失败日志去重：同一条错误 5 分钟内只警告一次，避免群聊里刷屏。
- 启动时主动提示 Cloudflare 渠道的计费限制。

---

## [1.0.1] - 2026-11-01

### 修复

- **Cloudflare 渠道端点错误**（`400 code 7000 No route for that URI`）。
  实测确认 Cloudflare 的两套路由**不等价**：

  | 请求 | 结果 |
  |---|---|
  | `/ai/run/typesafe/jev` | 400 `7000 No route for that URI` |
  | `/ai/run/@cf/typesafe/jev` | 400 `5007 No such model` |
  | `/ai/run` + `{model, input}` | ✅ 正确 |

  第三方模型必须走通用入口 `/ai/run`，模型名放请求体、参数包在 `input` 里。

### 新增

- `@cf/typesafe/*` 前缀误填时的自动纠正（不影响 Cloudflare 自托管模型）。
- HTTP 错误翻译为可操作提示：`402/2021` 明确指出免费 Neurons 不覆盖第三方模型；
  `7000` / `5007` / `401` / `429` 各有对应解释。

---

## [1.0.0] - 2026-11-01

首个版本。

### 新增

**私聊耐心合并**

- 用户连续打字时先不回复，等静默窗口结束后把整串消息合并成一条输入交给模型。
- 三层时间边界：自适应静默窗口（文本越长窗口越长）+ 从首条算起的整轮硬上限
  + 条数/字数溢出即刻结算。
- 生成中收到新消息则立刻中止旧生成，并清空旧结果链 —— 非流式下旧回复一个字都不发。
- 合并文本就地写回最新事件，**始终走 AstrBot 原生 LLM 流程**，
  人设、记忆、工具、会话历史全部保留。

**群聊 Jev 判定**

- 本地闸门（指令 / 自己 / 太短 / 限频 / 冷却 / 熔断）先于任何网络请求短路。
- 一次请求问三个是非题：`addressed_to_bot` / `open_question` / `already_answered_by_bot`。
- 决策策略写在代码里，调阈值不需要重新请求。
- 未被 @ 也可主动插话；被 @ 但刚答过则不重复。
- 失败策略可配，默认保守（不回复）；但被 @ 的消息**永远放行**。

**三个 Jev 渠道**

- TypeSafe 官方、Cloudflare Workers AI、OpenCode。
- 两个渠道的请求/响应差异由归一化层完全吸收，调用方拿到相同结构。

**工程**

- 纯逻辑层不依赖 astrbot，可离线单测。
- 面板可视化配置，保存即热重载，`terminate` 彻底清理。
- 诊断指令：`/betterchat status` / `gate <文本>`（干跑判定）/ `reset`。

### 已知限制

- 流式输出下打断不彻底：开启 `streaming_response` 后 AstrBot 会跳过
  `on_decorating_result`，只能尽快停止生成，已发出的片段收不回。
- 只合并纯文本消息；带图片/文件/语音/转发的直接放行。
- 保存配置会取消正在等待的合并窗口（热重载的固有代价）。

---

[未发布]: https://github.com/AS214933/astrbot-betterchat/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/AS214933/astrbot-betterchat/compare/v1.3.1...v1.4.0
[1.3.1]: https://github.com/AS214933/astrbot-betterchat/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/AS214933/astrbot-betterchat/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/AS214933/astrbot-betterchat/compare/v1.0.4...v1.2.0
[1.1.0]: https://github.com/AS214933/astrbot-betterchat/compare/v1.0.4...v1.2.0
[1.0.4]: https://github.com/AS214933/astrbot-betterchat/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/AS214933/astrbot-betterchat/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/AS214933/astrbot-betterchat/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/AS214933/astrbot-betterchat/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/AS214933/astrbot-betterchat/releases/tag/v1.0.0
