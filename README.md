# Fames_Tou

> **两个 AI 角色共用一个舞台 —— 让它们实时对话、轮流发言、互相接话的编排引擎。**

## 这个项目做什么

`fames` 和 `tou` 是两个 AI 角色，在同一场直播里共同主持：彼此进行实时语音对话、
互相打趣、回应观众弹幕、记住老观众，并且一起打游戏。

系统要解决的核心问题是**两个智能体如何共享同一个舞台而不互相打架**：

| 问题 | 解法 | 代码位置 |
| --- | --- | --- |
| 这一轮谁说话？ | 发言调度器：@提及优先，否则按轮换；观众消息进优先级队列 | `speaker_scheduler.py` |
| 两人会不会同时出声抢麦？ | 语音输出仲裁器：同一时刻只有一个声音持有"发言权" | `speech_output_arbiter.py` |
| 会不会把搭档的话当成自己说的？ | 跨角色历史镜像：对方发言以 `[对方说]` 形式作为舞台提示注入 | `conversation.py` |
| 记不记得住老观众？ | 长期记忆：SQLite + LLM 蒸馏 + 时间衰减 + 确定性事实守卫 | `memory/` |

## 快速体验（零配置）

```bash
python main.py
```

不需要任何 API key、音频硬件或模型。`main.py` 用两个示例角色驱动**真实的**协调核心
（事件总线、全局状态机、发言调度器、语音仲裁器全是生产代码），你能在终端里直接看到
轮流发言、@提及路由和"同时只有一个声音"的仲裁逻辑如何运行。LLM 与语音在这一路径下
被替换成占位实现，生产引擎在下面这些文件里。

## 它是怎么运作的

- **实时双会话引擎**（`realtime_chat.py`、`realtime_chat_protocol.py`）
  每个角色运行在独立的端到端语音对话会话上（doubao SC2.0 over websocket）；
  音频按说话人归因并路由，因此两个角色可以同时在线。
- **轮流发言编排与跨角色镜像**（`conversation.py`、`speaker_scheduler.py`）
  下一个谁说话，由 @提及、搭档交接和观众弹幕优先队列实时决定；一方说出的内容
  以舞台提示的形式镜像进另一方的上下文，避免任何一方把自己的搭档误认成自己。
- **语音输出仲裁**（`speech_output_arbiter.py`）
  同一时刻只有一个声音持有发言权（QUEUE / DROP / INTERRUPT 三种策略），
  且只有在角色的**音频真正播完**后才释放，确保两人不会互相压话。
- **语音合成**（`lumi_tts.py`、`cosyvoice_tts.py`、`tts_emitter.py`）
  流式文本转语音，支持音色克隆（CosyVoice），音色 ID 通过本地环境变量注入。
- **语音识别**（`lumi_asr.py`）—— 流式语音识别，用于实时语音输入。
- **长期记忆**（`memory/`）
  基于 SQLite 的观众记忆与自身记忆，由 LLM 蒸馏成事实与摘要，
  带时间衰减（过期话题自动降权而非删除）与确定性成员事实守卫。
- **游戏环境**（`games/`）
  把游戏改造成智能体的工具调用场景，让角色在解说的同时做决策、调工具：
  **恶魔轮盘**（回合制决策）、**泰拉瑞亚**（**A\* 寻路** + **五层目标规划器**，
  配套一个 focus-safe 的 tModLoader Mod）、**王国保卫战**（塔防 AI，其 LuaJIT Mod
  被逆向进游戏的 LÖVE 引擎，另配逐波 LLM 战略决策，见
  [逆向笔记](docs/games/kingdom-rush-reverse-engineering.md)），以及两款文字游戏
  **Wordle** 与 **汉兜**（中文成语版 Wordle）—— 各自是自包含的网页前端加一个运行在
  **独立 worker 进程**中的熵值求解器，重计算不会卡住主循环。
- **快脑**（`fast_brain.py`）
  每个角色一个轻量 LLM，负责工具调用与游戏决策，与实时语音链路并行工作。
- **直播复盘**（`stream_analytics.py`）
  纯事件总线订阅者：统计每个角色的发言次数/累计时长/占比、抢麦冲突
  （排队·丢弃·打断·优先级抢占·冷却拦截）、观众消息吞吐与状态时间线，
  下播后直接产出复盘报告——协调层不需要为"被统计"改一行代码。
- **协调骨架**（`event_bus.py`、`state_machine.py`）
  所有模块通过进程内事件总线通信，并锚定在唯一的全局状态机上。

完整设计见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 仓库结构

```
main.py                  # 零配置共播演示
event_bus.py             # 协调骨架：模块间唯一通信通道
state_machine.py         # 全局直播状态
speaker_scheduler.py     # 发言选择与 @提及路由
speech_output_arbiter.py # 同一时刻只有一个声音持有发言权
conversation.py          # 文本链路编排与历史镜像
realtime_chat*.py        # 双端到端实时语音会话
lumi_asr.py / lumi_tts.py / cosyvoice_tts.py / tts_emitter.py   # 语音输入输出
stream_analytics.py      # 直播复盘：发言分布与抢麦冲突统计
memory/                  # SQLite 记忆、抽取、衰减与确定性事实
games/                   # 每个游戏一个目录；文字游戏共用独立 solver worker
docs/                    # 架构文档与游戏工程笔记
tests/                   # 零依赖的公共核心自检
```

游戏入口见 [games/README.md](games/README.md)，完整数据流见
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 快速开始

协调层与 `main.py` 演示只依赖标准库。要运行真实语音引擎，安装依赖并提供你自己的密钥：

```bash
pip install -r requirements.txt
cp .env.example .env     # 然后填入你自己的模型与语音服务密钥
```

### 需要自备的东西

- **API 密钥**（只有跑真实语音才需要，`main.py` 演示完全不需要）
  文本大脑接受任何 **OpenAI 兼容**接口（doubao ARK、DashScope、OpenAI 等）；
  实时语音链路使用 **doubao SC2.0**，流式 TTS / ASR 使用 **DashScope**
  （CosyVoice / fun-asr）。密钥写在 `.env` 里。
- **你自己的音色 ID** —— 克隆音色注册表不随仓库发布。`voice_registry.py` 会从
  `CHARACTER_A_VOICE_ID` / `CHARACTER_B_VOICE_ID` 及可选的 `*_VOICE_MODEL`
  变量解析，未配置时 TTS 回退到系统音色。
- **Live2D 模型** —— 虚拟形象 / 动作 / 表情层与具体角色模型绑定，**不包含**在仓库内，
  需要接入你自己的模型。
- **游戏本体** —— 游戏桥接通过 TCP 与商业游戏通信，游戏本体需自备。
  泰拉瑞亚侧的 LumiBridge Mod 源码已包含在仓库内，由 bot 自动编译，
  安装说明见 [其配置指南](games/terraria/mod/README.md)。
- **角色人设** —— `voice_config.py` 里提供的是占位示例角色，
  把音色、Live2D 模型名与人设提示词替换成你自己的角色即可。

## 验证

公共核心的自检路径不依赖任何外部服务：

```bash
python main.py
python -m unittest discover -s tests -v
python -m compileall -q .
```

## 本项目的改动

本项目基于开源项目 **Lumi_Nox** 二次开发，在其架构之上做了以下改动：

1. **角色替换**：把上游的示例角色替换为本项目的两个角色 `fames` 与 `tou`，
   配置、提示词、控制台输出与游戏侧默认名全部对齐。
2. **新增直播可观测性模块**（`stream_analytics.py`）：一个纯事件总线订阅者，
   统计每个角色的发言次数 / 累计时长 / 占比，抢麦冲突（排队 · 丢弃 · 打断 ·
   优先级抢占 · 冷却拦截），观众消息吞吐与状态时间线，下播后直接产出复盘报告。
   协调层不需要为"被统计"改任何代码——这是事件驱动架构的直接红利。
3. **新增两种抢麦策略**（`speech_output_arbiter.py`）：
   - `POLICY_PRIORITY` 优先级抢占：付费互动（SC / 上舰 / 礼物）可以不等当前发言结束，
     直接把发言权拿过来，不会因为两个角色正在闲聊而被漏掉；
   - **连麦冷却**：同一角色交还发言权后的一段时间内不能立刻再开口，防止单个角色霸场；
     同时保留 `ignore_cooldown` 逃生通道，供"被点名必须回话"的场景使用。
4. **补充测试**：新增 `tests/test_arbiter_policies.py` 与 `tests/test_stream_analytics.py`
   覆盖上述新策略与统计模块，并修掉演示测试在 GBK 环境下解码 UTF-8 输出的脆弱点。

## 许可证与出处

上游项目：[**Lumi_Nox**](https://github.com/MIO-456/Lumi_Nox)（作者 **Mio**，MIT 协议）。

本项目的架构与实现沿用其成果，相关版权归原作者所有。
依据 MIT 协议，使用、修改、分发本项目时需保留版权声明与许可全文，全文见 [LICENSE](LICENSE)。
