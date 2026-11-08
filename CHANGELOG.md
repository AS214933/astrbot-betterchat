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

## [1.7.0] - 2026-11-08

### 新增

- **Command Code 渠道（GOAT 计划）**。

  Command Code 的 Provider API 提供 `/provider/v1/systemone`，与 TypeSafe
  官方的 System One **完全同构**（同样的 `{model, state, questions}` →
  `{answers}`），只是换了域名和鉴权 —— 所以请求体不需要任何转换，
  只多一个 transport。

  | 项 | 值 |
  |---|---|
  | 端点 | `https://api.commandcode.ai/provider/v1/systemone` |
  | 模型 | `typesafe/jev`（或 `typesafe/jev-1.14.0` 固定版本）|
  | 鉴权 | Studio 创建的 key（`user_` 开头）|
  | 需要 | **GOAT 或更高**（$10/mo 起）|

  错误提示针对该渠道专门处理：`403 upgrade_required` 会明确告知
  「Go 计划没有 API 权限，需 GOAT 及以上」。

### 修复

- **`_post` 被误改，导致整个渠道静默失效**（开发过程中自己引入的）。

  手工改代码时把 `_build_payload` 的分支误插进了 `_post`，
  使它在发请求前直接 `return` 了一个 dict。**所有单元测试依然全绿**，
  因为它们都 monkeypatch 了 `_post`。

  新增两个测试盯住这条路径：`_post` 必须真的走到 `session.post`，
  且四个渠道都要验证一遍。还专门注入同样的 bug 确认测试能抓住它。

---

## [1.6.0] - 2026-11-08

### 修复

- **合并时贴纸 / 图片被整个丢掉**（影响理解效果）。

  用户发「摩西摩西」再跟一个 🥰 贴纸时，正常人是一起看的。
  但我们只取了 `message_str` —— 贴纸的文本部分是空的，
  于是它在合并结果里**彻底消失**，Bot 完全感受不到语气。

  两处都有问题：

  | 位置 | 问题 |
  |---|---|
  | `merge_texts` | `if p` 过滤掉空字符串 → 贴纸被丢弃 |
  | `_replace_text` | `[Plain(text)]` 整个替换消息链 → 组件全丢 |

  现在合并窗口除了文本还记录**非文本组件**，结算时一起写回：

  ```python
  message_obj.message = [Plain("摩西摩西
在吗"), Image("sticker.png")]
  ```

  这样模型既看得到说的话，也看得到表情。

### 说明

- 只合并纯文本的限制也随之放开：图片、贴纸、文件都会跟着走。
  引用消息的引用文字会作为上下文并入，但 `Reply` 组件本身不放回链里
  （那会破坏回复关系）。

---

## [1.5.5] - 2026-11-03

### 修复

- **打断生成功能完全失效**（关键，且是 1.5.3 引入的回归）。

  1.5.3 为了不和 AstrBot 的 `follow_up` 抢消息，把
  `_follow_up_will_take()` 的检查放在了 `_merge_turn()` **之前**：

  ```python
  if self._follow_up_will_take(umo, event):
      return          # ← 打断逻辑在 _merge_turn 里，根本走不到
  ```

  而 `follow_up` 的条件是「该会话有活跃 runner 且发送者相同」——
  **只要 Bot 在生成，用户发的每一条都会命中**，于是永远让路，
  永不打断。用户诉求「生成中发新消息就停掉旧回复」形同虚设。

  修复后的规则：

  | 旧轮所处阶段 | 行为 |
  |---|---|
  | 还在合并窗口里 | 合并（不打断） |
  | 已开始生成 | **打断**（立即重来） |

  由 `_is_generating()`（看 `EXTRA_TICKET` 标记）区分。

  同时补上一点：**打断时不等待合并窗口**。用户刚说完新的一句，
  期待的是马上重来；如果再等 4 秒窗口，等于白打断。

- `follow_up` 让路现在只在**打断关闭时**生效（作为"不和框架抢消息"的
  降级路径），不再无条件优先。

### 说明

- 这条是 1.5.3 的回归，根因是修 A（缺最后一句）时没意识到
  会掐死 B（打断）。两个需求都要，靠「旧轮处于哪个阶段」区分。

---

## [1.5.4] - 2026-11-03

### 变更

- **合并窗口的默认值按真实打字节奏重新调整**：

  | 参数 | 旧 | 新 |
  |---|---|---|
  | `base_quiet_seconds` | 2.0 | **4.0** |
  | `min_quiet_seconds` | 1.5 | **2.5** |
  | `max_quiet_seconds` | 4.0 | **6.0** |
  | `total_seconds` | 8.0 | **12.0** |

  原因是实测出来的：用户连发消息的间隔从 0.7 秒到 8.6 秒都有。
  旧的 2 秒窗口下，间隔 3.57 秒的第二句就漏在窗口外了 ——

  ```
  22:49:54  不知道...        → 进窗口，2 秒后单独结算
  22:49:57  感觉被别人抛了    → 开新窗口
  22:49:59  QAQ             → 又被 follow_up 接走
  ```

  结果同一段话被拆成**三轮回复**，内容还各说各的。

  新默认值下同样三条消息会全部合并成一轮（窗口 4.1 秒 > 3.57 秒间隔）。

### 说明

- 这不是 bug，是参数和真实使用节奏不匹配。窗口调大后「说完一句就得等 4 秒」，
  如果你觉得慢，可以在面板把 `base_quiet_seconds` 调回 2~3 秒 ——
  代价是偶尔把一句话拆成两轮。

---

## [1.5.3] - 2026-11-03

### 修复

- **合并文本缺了最后一句，Bot 回「话只说一半」**。

  实测：用户连发 4 条（Sticker / 那你能 / 抱抱我吗 / QAQ）。

  ```
  22:41:14  那你能     → 进我们窗口
  22:41:15  LivingMemory 收到 query='那你能'   ← 第一轮已跑起来
  22:41:17  QAQ       → 被 AstrBot 的 follow_up 接走
  22:41:20  我们合并 3 条                       ← 缺了 QAQ
  ```

  根因：AstrBot 的 `follow_up` 在我们的钩子**之前**执行
  （`internal.py` 里 `try_capture_follow_up` 在
  `OnWaitingLLMRequestEvent` 之前），条件是「该会话有活跃 agent runner
  且发送者相同」。被它接走的消息**根本到不了我们这里**，于是我们攒的
  那一份少了最后一句。

  修复：新增 `_follow_up_will_take()`，检测到 follow_up 会接管时
  **直接让路**，不参与合并。

- 顺带记录一个设计取舍：也让路给了 **follow_up**，而不是无脑打断。
  AstrBot 已经用 follow_up 处理「生成中的补充消息」，我们再攒一份就是重复；
  而无脑打断会浪费已生成的 token，且用户打错字补一句时整轮都作废。

### 说明

- 这个 bug 是 1.5.2 修好「窗口内不互相打断」之后才暴露的：
  以前窗口根本活不下来，也就看不出少了一句。

---

## [1.5.2] - 2026-11-03

### 修复

- **私聊连发时每条消息各自回复一次**（关键）。合并彻底失效。

  根因是打断逻辑判断过头了。它只看「上一轮还在不在 `in_flight` 里」，
  而 `in_flight` 从**合并窗口开始**就登记、直到消息发送后才清理 ——
  于是「还在窗口里等」的上一轮被误判成「正在生成」，直接被 `stop_event`。

  连锁反应：

  ```
  用户发 A → A 进窗口 → B 到来 → 打断 A → B 成为新 in_flight
                    → C 到来 → 打断 B → C 成为新 in_flight → ...
  ```

  结果是每条消息都把前一条打断，合并窗口一次都没成功结算过。

  修复：新增 `_is_generating()`，用 `EXTRA_TICKET` 标记区分阶段 ——
  只有**真正结算过**（拿到本轮、写过合并文本）的事件才允许被打断。
  还在窗口里等的一律走合并。

  - 连发三条 → 只回复一次（新增回归用例）
  - 生成中仍可被新消息打断（核心功能保留，新增回归用例）

### 说明

- 这次的 bug 是上一次修复（1.5.1 让非文本消息也进窗口）暴露出来的：
  以前表情会绕过窗口，掩盖了「窗口内的消息互相打断」这个问题。

---

## [1.5.1] - 2026-11-03

### 修复

- **私聊：非纯文本消息绕过合并窗口，导致一次行为产生两个回复**。

  实测场景：用户连发「有你就够了」+「欸嘿」+ 一个 Sticker。

  ```
  14:57:30  有你就够了      → 进合并窗口
  14:57:31  欸嘿            → 刷新窗口（2 条待合并）
  14:57:34  Sticker         → is_plain_text=False → 直接放行
  14:58:06  才发出回复       ← 距第一条 36 秒
  ```

  Sticker 绕过窗口单独触发了一次 LLM 请求，而攒着的两句到期后又触发
  第二次 —— 两个回复互相排队，延迟被拖到 30 秒以上。

  现在非文本消息也进入合并窗口（只是不参与文本拼接），一轮只有一个出口。

- **Jev 返回空响应体时完全无法诊断**。某些 400 只给状态码不给 body，
  日志里只剩 `HTTP 400: `，看不出是哪个字段的问题。

  现在会补上请求侧摘要：`status` / `channel` / `model` / `endpoint` /
  `state_chars` / `questions`。

- **冷场检查把 Jev 不可用当成意外异常**，打印完整堆栈。Jev 故障、熔断、
  超时都是**预期内的降级场景**，不该刷堆栈（看着像插件崩了）。
  现在走统一的降级日志路径。

### 说明

- 测试里一处浮点断言改用 `pytest.approx`（`29.999999999992724 != 30`），
  是测试本身的精度问题，与实现无关。

---

## [1.5.0] - 2026-11-02

### 变更

- **「主动帮没人接的问题」由「立即帮」改为「观望后再帮」**。

  1.4.2 里这条规则被直接关掉了，因为立即插话会和正在打字的人撞车 ——
  群里两个人正常对话时「有人提问、还没人回答」是最常见的状态。

  现在改成观望：

  ```
  发现问题没人接
      ↓
  观望 N 秒（默认 90）      ← 期间有人接话就取消
      ↓
  仍然没人答 → 主动帮一句
  ```

  新增配置 `group.help_unaddressed_delay_seconds`（默认 90 秒，0 表示不观望）。
  `group.help_unaddressed` 恢复默认开启 —— 能力保留，只是不再莽撞。

- 判定新增 `DEFER` 动作：表示「现在不该插话，但值得等一会儿」。
  与 `IGNORE` 的区别是它会在观望期满后触发一次主动发言。

### 说明

- 观望期满后仍会走完整的本地闸门（免打扰 / 每日上限 / Bot 冷却），
  不是无条件发言。

---

## [1.4.2] - 2026-11-02

### 修复

- **1.4.1 的 @别人 修复在 QQ 官方平台上完全没生效**。这是个很隐蔽的坑：
  AstrBot 的 `qqofficial` 适配器**只把 @Bot 自己**的 mention 转成 `At` 组件，
  @ 别人的 mention 被原样留在 `message_str` 里，形如 `<@openid>`：

  ```python
  bot_mentions = [m for m in message.mentions if m.is_you is True]
  if group_mentioned:
      msg.append(At(qq=abm.self_id, ...))
  ```

  所以只遍历 `At` 组件的实现，在这个平台上**永远返回 False**。单测能过
  （构造的假消息链里塞了 `At`），线上却完全不生效。

  改为**双轨检测**：先查 `At` 组件（通用平台），再匹配文本里的
  `<@...>` / `<@!...>` / `<qqbot-at-user id="..." />`（QQ 官方）。

- **「主动帮没人接的问题」太激进**。这条规则让 Bot 见话就接：
  群里两个人正常对话时「有人提问、还没人回答」是最常见的状态，
  `open_question` 必然很高，于是每条都插话。

  改为**默认关闭**，需要的用户在面板里手动开启。
  @ 了别人的消息无论开关都不抢答。

### 说明

- 横向对比过：`astrbot_plugin_reply_gate` 只检测 @Bot、不处理 @别人；
  `astrbot_plugin_smart_wake` 的 `ignore_at_other` 同样只查 `At` 组件，
  在 QQ 官方平台上也是空开关。所以这条没有可直接借鉴的现成实现。

---

## [1.4.1] - 2026-11-02

### 修复

- **@ 了别人的消息会被抢答**（关键）。群里 A 问 B「你这是哪个大学的」时，
  从 Jev 的角度看就是「有人提问、没人回答」—— `open_question=0.95`、
  `addressed=0.06`，正好命中「主动帮一把」规则，Bot 便插进了两人之间的对话。

  根因：`state` 里只标了「有没有 @ Bot」，**没标「有没有 @ 别人」**。
  Jev 无从知道那是定向提问还是公开求助。

  修复分两层：
  - `state.bot` 新增 `addressed_to_other` 字段（平台层面已确定的事实）；
  - 「主动帮一把」规则加上前置条件：@ 了别人时不抢答。
    被 @ 的消息不受影响 —— 用户明确叫了 Bot 就该回。

- **主动开口时把"写作要求"复述进回复**。实测模型会发出这样的内容：

  > ...点出群名缩写就行。简短自然。

  「简短自然」是给模型的要求，却被当成正文吐了出来。原因是 prompt 写成了
  「要求：1. 2. 3.」的清单式，模型把它当成"我该怎么做"的说明。
  改为直接陈述 + 明确要求不复述、不解释，并给一个真正的退出选项。

### 说明

- 日志中同一行重复三次（含 `[Core]` 的事件总线、follow_up、工具调用日志）
  是 AstrBot 日志层或运行多份实例的表现，与本插件无关。

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

[未发布]: https://github.com/AS214933/astrbot-betterchat/compare/v1.5.5...HEAD
[1.5.5]: https://github.com/AS214933/astrbot-betterchat/compare/v1.5.4...v1.5.5
[1.5.4]: https://github.com/AS214933/astrbot-betterchat/compare/v1.5.3...v1.5.4
[1.5.3]: https://github.com/AS214933/astrbot-betterchat/compare/v1.5.2...v1.5.3
[1.5.2]: https://github.com/AS214933/astrbot-betterchat/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/AS214933/astrbot-betterchat/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/AS214933/astrbot-betterchat/compare/v1.4.2...v1.5.0
[1.4.2]: https://github.com/AS214933/astrbot-betterchat/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/AS214933/astrbot-betterchat/compare/v1.4.0...v1.4.1
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
