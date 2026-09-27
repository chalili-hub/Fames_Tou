"""launcher — lumi.py 的装配层。

拆分原则：`lumi.py` 只做「参数 + 装配 + 启动 + 退出」，其余职责都在这里：

    config.py     参数、.env/config.py 合并、启动体检（doctor）
    registry.py   可变运行时状态收口（替代上游散落的模块级全局）
    helpers.py    文本工具 / 工具执行 / 打断监听 / 游戏锚点
    context.py    ConversationContext 装配（35 个字段，含 lazy 绑定）
    audio.py      声卡枚举 + 语音层（realtime_chat / lumi_tts / ASR）初始化
    inputs.py     观众输入源（stdin / 平台适配器接口）+ 入队与落库
    loops.py      主循环（取输入 → 对话 → 推进）
    lifecycle.py  启动自检、优雅退出、记忆蒸馏、复盘报告
    dry_run.py    假大脑 / 假声卡（零依赖验证装配与主循环）

为什么要有 registry：上游用「模块级全局变量 + 上下文换入换出」承载会话状态，
这是它最大的技术债（曾在上下文外读到错误角色、把 A 的音频推进 B 的通道）。
本层不复制这个模式——可变状态只在一个显式对象里，且**不承载 per-session 数据**。
"""
