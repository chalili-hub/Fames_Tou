"""端到端实时语音（SC2.0）凭证探针。

**为什么需要它**：拿到 App ID / Access Key 之后，如果直接去跑整场直播，
失败信息会淹没在几百行日志里。这个探针只做三步，每步失败都给出可执行的判断：

    1. wss 建连（带 4 个鉴权头）  → 失败多为凭证/开通状态问题（看 HTTP 码）
    2. StartConnection(1) 应回 50  → 失败说明鉴权头缺项或被拒
    3. （可选）StartSession(100) 应回 150 → 能验证 model 版本与 speaker（音色）是否有效

鉴权头与 URL 一律从 `realtime_chat` 的常量取，避免两处漂移：

    X-Api-App-ID      = 你申请的 App ID
    X-Api-Access-Key  = 你申请的 Access Key
    X-Api-Resource-Id = volc.speech.dialog      （固定）
    X-Api-App-Key     = 公开 App Key            （固定）
    X-Api-Connect-Id  = 每次随机 uuid           （固定做法）
"""
from __future__ import annotations

import asyncio
import os
import uuid

MASK_KEEP = 4


def _mask(secret: str) -> str:
    s = secret or ""
    if len(s) <= MASK_KEEP * 2:
        return "*" * len(s)
    return f"{s[:MASK_KEEP]}...{s[-MASK_KEEP:]}（长度 {len(s)}）"


def _resolve_credentials(app_id=None, access_key=None):
    app_id = app_id or os.environ.get("VOLC_DIALOG_APP_ID") or ""
    access_key = access_key or os.environ.get("VOLC_DIALOG_ACCESS_KEY") or ""
    return app_id.strip(), access_key.strip()


def _headers(app_id: str, access_key: str) -> dict:
    import realtime_chat as rt          # 常量单一来源
    return {
        "X-Api-App-ID": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": rt.RESOURCE_ID,
        "X-Api-App-Key": rt.APP_KEY,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }


def _diagnose_http(code: int) -> str:
    return {
        401: "App ID 与 Access Key 不匹配（注意复制时是否带了空格/换行；二者必须来自同一个应用）",
        403: "该应用没有开通「端到端实时语音大模型」权限，或未实名 / 已欠费 / 并发已满 / 触发了 IP 白名单",
        404: "URL 或 X-Api-Resource-Id 不对（本仓库用的是固定值，通常不会出现）",
        429: "超出并发或 QPS 限制",
    }.get(code, f"未预期的 HTTP {code}，建议对照官方文档的错误码表")


def probe_ark_key(api_key: str = "", *, model: str = "", log_fn=print) -> int:
    """实测方舟（ARK）Key 是否可用 —— 发一次 1-token 的 chat 调用。

    **为什么必须实测**：方舟的 Key 与语音（duplex）的 Key **都是 36 位 UUID 形态**，
    肉眼看不出区别；而且方舟的 base_url 与语音端点完全不同
    （`ark.cn-beijing.volces.com/api/v3` vs `openspeech.bytedance.com`）。
    实测踩过的坑：把语音 Key 填到 `ARK_API_KEY_FAST`，会得到
    `401 AuthenticationError: The API key doesn't exist`。
    """
    import os
    key = (api_key or os.environ.get("ARK_API_KEY_FAST") or "").strip()
    if not key:
        log_fn("  · ARK key 未设置（ARK_API_KEY_FAST）→ 快脑无法生成台词，"
               "只能跑 `--dry-run` 的占位台词")
        return 1
    try:
        import fast_brain                      # 复用模型注册表，避免模型名漂移
        model_id = model or fast_brain.LLM_MODEL
        base_url = "https://ark.cn-beijing.volces.com/api/v3"
    except Exception:
        model_id = model or "doubao-seed-2-0-lite-260428"
        base_url = "https://ark.cn-beijing.volces.com/api/v3"
    try:
        from openai import OpenAI
    except Exception as e:
        log_fn(f"  · 未安装 openai，跳过 ARK 实测：{e}")
        return 1
    client = OpenAI(api_key=key, base_url=base_url)
    try:
        r = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=4,
        )
        text = (r.choices[0].message.content or "").strip()
        log_fn(f"  ✓ ARK key 可用（model={model_id}，回包={text[:20]!r}）")
        return 0
    except Exception as e:
        name = type(e).__name__
        msg = str(e).replace("\n", " ")[:220]
        log_fn(f"  ✗ ARK key 不可用：{name} —— {msg}")
        if "doesn't exist" in msg or "AuthenticationError" in name:
            log_fn("    处置：这多半不是方舟的 Key（可能是语音产品的 Key）。"
                   "去**方舟控制台 → API Key 管理**新建，填到 .env 的 ARK_API_KEY_FAST")
        else:
            log_fn(f"    处置：Key 可能是方舟的，但模型 {model_id} 未开通/未创建接入点；"
                   "去方舟控制台开通对应模型，或用 --model 指定已开通的模型")
        return 1


async def _probe_async(*, app_id, access_key, timeout, with_session, voice_id, log_fn):
    import realtime_chat as rt
    from realtime_chat_protocol import (build_event_frame, build_event_frame_no_session,
                                        parse_response)
    try:
        import websockets
    except Exception as e:
        log_fn(f"✗ 未安装 websockets：{e}")
        return 1

    url = rt.WS_BASE_URL
    headers = _headers(app_id, access_key)
    log_fn(f"[探针] URL        : {url}")
    log_fn(f"[探针] Resource-Id: {headers['X-Api-Resource-Id']}（固定）")
    log_fn(f"[探针] App-Key    : {headers['X-Api-App-Key']}（公开固定值）")
    log_fn(f"[探针] App ID     : {_mask(app_id)}")
    log_fn(f"[探针] Access Key : {_mask(access_key)}")

    for label, val in (("VOLC_DIALOG_APP_ID", app_id), ("VOLC_DIALOG_ACCESS_KEY", access_key)):
        if not val:
            if label == "VOLC_DIALOG_APP_ID":
                log_fn("✗ VOLC_DIALOG_APP_ID 为空 —— 该产品需要「App ID + Access Key」**一对**，"
                       "只填其中一个连不上。")
                log_fn("   去哪找 App ID：")
                log_fn("     · 控制台「STEP1 获取 API Key」区域通常同时显示 App ID；")
                log_fn("     · 或点开「完整调用指南」，看示例代码里 X-Api-App-ID 的值"
                       "（形如一串数字，例如 1234567890）；")
                log_fn("     · 注意它必须与 Access Key 来自**同一个项目**（混用必 401）。")
                log_fn("   填进 .env 的 VOLC_DIALOG_APP_ID 后重跑：python lumi.py --probe")
            else:
                log_fn(f"✗ {label} 为空：先按 docs/CREDENTIALS.md 申请后写进 .env")
            return 1

    # ── 1) 建连 ────────────────────────────────────────────────────────
    try:
        try:
            ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers=headers,
                                   ping_interval=None, proxy=None),
                timeout=timeout,
            )
        except TypeError:
            # 兼容老版 websockets 的参数名
            ws = await asyncio.wait_for(
                websockets.connect(url, extra_headers=headers,
                                   ping_interval=None, proxy=None),
                timeout=timeout,
            )
    except Exception as e:
        name = type(e).__name__
        if name == "InvalidStatus":
            code = getattr(getattr(e, "response", None), "status_code", 0)
            log_fn(f"✗ 建连被拒：HTTP {code} —— {_diagnose_http(code)}")
        elif isinstance(e, asyncio.TimeoutError):
            log_fn(f"✗ 建连超时（{timeout}s）：网络到 openspeech.bytedance.com 不通。"
                   f"注意引擎显式设置 proxy=None（不走系统代理），如果你在公司网络里可能需要放行")
        else:
            log_fn(f"✗ 建连失败：{name}: {e}")
        return 1

    log_fn("✓ 步骤 1/3：WebSocket 建连成功（鉴权头已被服务端接受）")
    try:
        # ── 2) StartConnection ────────────────────────────────────────
        await ws.send(build_event_frame_no_session(event_id=1, payload={}))
        resp = parse_response(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if resp.get("event") != 50:
            log_fn(f"✗ 步骤 2/3：StartConnection 未收到事件 50，实际响应：{resp}")
            return 1
        log_fn("✓ 步骤 2/3：StartConnection 收到事件 50")

        if not with_session:
            log_fn("（跳过 StartSession；加 --probe-session 可进一步验证 model 版本与音色）")
            return 0

        # ── 3) StartSession（验证 model / speaker）────────────────────
        cfg = rt.SessionConfig(character_manifest="", voice_id=voice_id or "")
        payload = rt._build_start_session_payload(cfg)
        log_fn(f"[探针] StartSession payload 摘要：model="
               f"{payload.get('dialog', {}).get('extra', {}).get('model')} "
               f"speaker={voice_id or '(空)'}")
        await ws.send(build_event_frame(event_id=100, session_id=str(uuid.uuid4()), payload=payload))
        resp = parse_response(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if resp.get("event") == 150:
            dialog_id = (resp.get("payload_msg") or {}).get("dialog_id")
            log_fn(f"✓ 步骤 3/3：StartSession 收到事件 150，dialog_id={dialog_id}")
            log_fn("🎉 凭证与音色都可用，可以跑 `python lumi.py --arch realtime`")
            return 0
        code = resp.get("code")
        log_fn(f"✗ 步骤 3/3：StartSession 失败：{resp}")
        if code:
            log_fn("   常见原因：model 版本（本仓库固定 2.2.0.0 = SC2.0）与该应用开通的版本不一致；"
                   "或 speaker（音色 ID）不存在/未授权")
        else:
            log_fn("   常见原因：音色 ID 无效（tts.speaker），或该应用未开通对应音色")
        return 1
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def probe_credentials(*, app_id=None, access_key=None, timeout: float = 12.0,
                      with_session: bool = False, voice_id: str = None,
                      log_fn=print) -> int:
    """返回 0 = 凭证可用；1 = 不可用（并已打印原因与处置建议）。"""
    app_id, access_key = _resolve_credentials(app_id, access_key)
    if voice_id is None:
        try:
            from voice_config import get_speaker_config
            voice_id = get_speaker_config("fames").realtime_voice_id or ""
        except Exception:
            voice_id = ""
    log_fn("=" * 68)
    log_fn("端到端实时语音（SC2.0）凭证探针")
    log_fn("=" * 68)
    try:
        return asyncio.run(_probe_async(app_id=app_id, access_key=access_key,
                                        timeout=timeout, with_session=with_session,
                                        voice_id=voice_id, log_fn=log_fn))
    except KeyboardInterrupt:
        log_fn("（被中断）")
        return 1
