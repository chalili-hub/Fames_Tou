"""干跑：假大脑 + 假声卡，但驱动**真实的**协调层。

这是本层的"可自证"手段，思路与上游 `main.py` 一致：
    真实：EventBus / StateMachine / SpeakerScheduler / SpeechOutputArbiter /
          StreamAnalytics / ConversationContext / chat_and_speak / 记忆写入
    假的：LLM（返回台词）、音频设备（不打开）、TTS（关掉）

所以干跑跑通 ≈ 装配与主循环跑通，不需要任何 API key、声卡、网络。

假 LLM 的注入点：`_stream_llm_text_only` 走的是
`fast_brain.resolve_call_target(...)`（conversation.py:469），
所以打这个函数就能接管生成，且**不需要改动上游任何一行代码**。
"""
from __future__ import annotations

import json
import re
import types

from launcher.registry import RUNTIME

# 干跑台词池（不是角色人设——人设属于未开源资产）
DRY_BANTER = (
    "欢迎来到直播间，今晚就我们几个，慢慢聊。",
    "这条弹幕有点意思，我认真接一下。",
    "话说回来，今天的状态还不错，嗓子也顺。",
    "你们别刷那么快，我一条一条看不过来。",
    "行，那就这么定了，下一段咱们换个话题。",
)


class FakeLLMClient:
    """够用的 OpenAI 兼容客户端替身：只实现 chat.completions.create(stream=True)。

    必须忠实于 `_stream_llm_text_only` 读取的字段：`chunk.usage`、`chunk.choices`、
    `chunk.choices[0].delta.content`（可选 `.tool_calls`）。
    """

    def __init__(self, *, delay: float = 0.0, echo_user: bool = True):
        self._n = 0
        self._delay = delay
        self._echo_user = echo_user
        self.chat = types.SimpleNamespace(completions=self)

    # 与 openai SDK 的调用形态一致：client.chat.completions.create(**kwargs)
    def create(self, **kwargs):
        messages = kwargs.get("messages") or []
        last_user = ""
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                last_user = str(m.get("content") or "")
                break
        # 去掉上下文前缀（"[mona说] xxx" / "[直播提示] xxx"），只留正文
        last_user = re.sub(r"^\s*\[[^\]]{1,12}\]\s*", "", last_user)
        last_user = last_user.replace("\n", " / ")[:24]

        # 有工具就发一次工具调用：游戏决策路径必须有它，
        # 否则无法验证「快脑决策 → game_request.result 回填 → 桥接执行」这条链路。
        tool_call = _pick_tool_call(kwargs.get("tools") or [])
        if tool_call:
            body = "行，这把我来。"
        elif self._echo_user and last_user:
            body = f"关于「{last_user.strip()}」，我说两句。"
        else:
            body = DRY_BANTER[self._n % len(DRY_BANTER)]
        self._n += 1
        # 非流式调用（隐式缓存预热走这条）：真实 SDK 返回的 ChatCompletion 带 .usage，
        # 而 stream=True 返回的是迭代器。这里必须区分，否则预热线程会报
        # "'generator' object has no attribute 'usage'"（实测踩过）。
        if not kwargs.get("stream"):
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=body))],
                usage=types.SimpleNamespace(prompt_tokens=64, completion_tokens=1,
                                            prompt_tokens_details=None),
            )
        return self._stream(body, tool_call=tool_call)

    def _stream(self, body: str, *, tool_call: dict = None):
        import time
        # 先"决定"再"解说"——与真实模型先出 tool_call 的行为一致
        if tool_call:
            yield _tool_chunk(tool_call)
        for i in range(0, len(body), 6):
            if self._delay:
                time.sleep(self._delay)
            yield _chunk(content=body[i:i + 6])
        yield _chunk(usage=types.SimpleNamespace(
            prompt_tokens=128, completion_tokens=len(body), prompt_tokens_details=None,
        ))


def _chunk(content=None, tool_calls=None, usage=None):
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choices = [types.SimpleNamespace(delta=delta)] if content else []
    return types.SimpleNamespace(choices=choices, usage=usage)


def _tool_chunk(tool_call: dict):
    """构造一个 tool_call 分片（content 为空但 choices 非空）。

    注意不能复用 `_chunk()`：那里对空 content 会产出 `choices=[]`，
    而 `_stream_llm_text_only` 见到空 choices 会直接 continue，工具调用就被吃掉了。
    """
    fn = types.SimpleNamespace(name=tool_call["name"], arguments=tool_call["arguments"])
    tc = types.SimpleNamespace(index=0, function=fn)
    delta = types.SimpleNamespace(content=None, tool_calls=[tc])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)], usage=None)


def _pick_tool_call(tools: list) -> dict | None:
    """从工具定义里挑一个**合法**的调用（优先带 enum 的参数，值取枚举第一项）。"""
    for t in tools or []:
        fn = (t or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        for key, spec in (params.get("properties") or {}).items():
            enum = (spec or {}).get("enum")
            if isinstance(enum, list) and enum:
                return {"name": name,
                        "arguments": json.dumps({key: enum[0]}, ensure_ascii=False)}
        required = params.get("required") or []
        if required:
            return {"name": name,
                    "arguments": json.dumps({required[0]: "1"}, ensure_ascii=False)}
    return None


def install_fake_llm(*, log_fn=print, echo_user: bool = True) -> FakeLLMClient:
    """接管 LLM 调用（打补丁，不改上游代码）。

    ⚠️ 必须同时打**两个**地方，否则干跑会漏出真实网络请求：
      1. `fast_brain.resolve_call_target` —— 对话生成走这里（conversation.py:469）；
      2. `fast_brain.llm_client` / `LLM_MODEL` —— **隐式缓存预热**走的是模块级全局
         （conversation._send_warmup_request 直接用 fast_brain.llm_client），
         不经过 resolve_call_target。实测漏掉这一步会打真实网络请求并刷 401。
    """
    import fast_brain
    client = FakeLLMClient(echo_user=echo_user)
    real_brand_params = fast_brain._brand_params

    def _resolve_call_target(needs_tools: bool = False):
        return client, "dry-run-fake-model", real_brand_params()

    fast_brain.resolve_call_target = _resolve_call_target
    fast_brain.llm_client = client
    fast_brain.LLM_MODEL = "dry-run-fake-model"
    # 让每角色 FastBrain 的 get_brand_params() 仍能拿到完整参数（logit_bias / extra_body 等）
    log_fn("[干跑] 已接管 LLM：resolve_call_target + llm_client + LLM_MODEL → FakeLLMClient")
    return client


def scripted_items():
    """干跑用的观众输入：直接复用真实路径的默认脚本，避免两处不一致。"""
    from launcher.inputs import DEFAULT_SCRIPT
    return list(DEFAULT_SCRIPT)


def _install_placeholder_credentials(log_fn):
    """干跑的密钥占位。

    为什么必须做这一步：`fast_brain.py:28` 在**模块导入时**就构造
    `OpenAI(api_key=os.getenv("ARK_API_KEY_FAST"), ...)`，而新版 openai SDK
    在 api_key 为空时**直接抛 OpenAIError**（不是懒加载）。
    于是"没有 key 也能跑装配"这条路径被导入期就挡死了。

    这里塞占位值而不是改上游文件：干跑全程由 FakeLLMClient 接管生成，
    根本不会发起网络请求，所以占位值不会带来任何假阳性——
    真实运行时仍要求真 key（见 lumi.py 的启动体检）。
    """
    import os
    placeholders = {
        "ARK_API_KEY_FAST": "dry-run-placeholder",
        "DASHSCOPE_API_KEY": "dry-run-placeholder",
        "VOLC_DIALOG_APP_ID": "dry-run-placeholder",
        "VOLC_DIALOG_ACCESS_KEY": "dry-run-placeholder",
    }
    # 注意：不能用 setdefault —— `.env` 里常见的形态是 `ARK_API_KEY_FAST=`（键存在、值为空），
    # 此时 setdefault 认为"已存在"而不覆盖，于是仍然拿空 key 去构造客户端而报错。
    filled = []
    for k, v in placeholders.items():
        if not os.environ.get(k):
            os.environ[k] = v
            filled.append(k)
    if filled:
        log_fn(f"[干跑] 已为 {len(filled)} 个缺失/空值密钥填占位值（不会发起真实请求）")


def run_dry(cfg, *, log_fn=print) -> int:
    """跑一场干跑直播。返回轮数。"""
    log_fn("=" * 68)
    log_fn("干跑模式：真实协调层 + 假大脑 + 假声卡（不需要任何 key / 声卡）")
    log_fn("=" * 68)
    # ★ 必须排在所有重模块导入之前：launcher.assemble → launcher.context →
    #   conversation → fast_brain，而 fast_brain 在导入期就构造 OpenAI 客户端。
    _install_placeholder_credentials(log_fn)

    from launcher.assemble import build_engine
    from launcher.inputs import ScriptedSource
    from launcher.loops import main_loop
    from launcher import lifecycle

    eng = build_engine(cfg, log_fn=log_fn)
    install_fake_llm(log_fn=log_fn)

    eng.source = ScriptedSource(scripted_items(), interval=0.05, delay=0.2,
                               scheduler=eng.scheduler, analytics=eng.analytics,
                               log_fn=log_fn).start()

    idle_before = RUNTIME.last_viewer_message_at
    try:
        eng.turns_played = main_loop(ctx=eng.context, scheduler=eng.scheduler,
                                     runtime=RUNTIME, cfg=cfg, log_fn=log_fn)
    finally:
        RUNTIME.stop_event.set()
        lifecycle.shutdown(eng, log_fn)
    return eng.turns_played
