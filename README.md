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

### 两个渠道

| | TypeSafe 官方 | Cloudflare Workers AI |
|---|---|---|
| 端点 | `POST api.typesafe.ai/v1/systemone` | `POST api.cloudflare.com/client/v4/accounts/{id}/ai/run` |
| 模型位置 | 请求体的 `model` | 请求体的 `model`，参数包在 `input` 里 |
| 响应 | `{model, answers, usage}` | 外面套一层 `{result, success, errors}` |
| 需要 | API Key | 账号 ID + API Token |

> ⚠️ **Cloudflare 的免费额度不覆盖第三方模型。**
> Workers AI 每天送的 10,000 Neurons 只适用于 Cloudflare **自托管**的 `@cf/` 模型
> （Llama、Qwen、Whisper 等）。`typesafe/jev` 属于第三方模型，走 AI Gateway
> **统一计费**，需要先充值预付额度，否则会返回
> `HTTP 402 / code 2021 Insufficient balance`。
>
> 想零成本使用，请选 **TypeSafe 官方**渠道（Jev 本身按 $0.042/百万输入 token
> 计费，输出免费，日常群聊基本花不到钱）。

> 📌 **Cloudflare 的 URL 形状容易踩坑。** 第三方模型**不能**写成
> `/ai/run/typesafe/jev`（会返回 `400 code 7000 No route for that URI`），
> 也不能带 `@cf/` 前缀（会返回 `400 code 5007 No such model`）。
> 正确写法是 `/ai/run` + 请求体 `{"model": "typesafe/jev", "input": {...}}`。
> 本插件已按此实现；如果你手填了 `@cf/typesafe/jev`，会自动纠正。

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
