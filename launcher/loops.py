"""主循环。

上游把「一轮对话」的全部逻辑封在 `conversation.chat_and_speak` /
`proactive_speak` 里——它们内部已经完成了：
    申请发言权 → 生成 → 逐句 TTS → 等真正播完 → mark_done → 镜像给搭档 → 推进轮换
所以本层的主循环很薄，只负责：
    1. 把队列里的观众输入取出来，组装成本轮的入参
    2. 调用对话函数（并兜异常，单轮失败不能拖垮直播）
    3. 空闲够久就主动说话
    4. 检查停止条件

⚠️ 关键：**不能在这里 sleep 很久**。打断靠 200ms 粒度轮询
（`lumi_tts.speak` 在等 TTS 时轮询 `tts_state.interrupted`），
主循环本身也用短睡 + stop_event，保证 Ctrl-C 和 /quit 能秒退。
"""
from __future__ import annotations

import time

from launcher.registry import RUNTIME


def _build_turn_inputs(items, *, session_id: str):
    """把队列里的一批观众消息组装成 chat_and_speak 的入参。

    - `user_input`：**整批正文合并成一段**。原因有两个：
      ① `speaker_scheduler.pop_all_inputs` 的文档语义就是"合并成一轮喂给 LLM"，
         洪流下不能让模型只看到最后一条；
      ② `chat_and_speak` 内部用**同一个字符串**做 @ 路由（`pick_speaker(user_input)`），
         如果只传最后一条，批次里其它条目的 @点名就会被丢掉
         ——干跑实测过：`rin: tou讲个冷笑话` 与 `kane 的 SC` 同批时，
         只传最后一条会让 @tou 失效。
      （多条里 @ 了不同角色时，`detect_addressed_speaker` 会返回 None 并按轮换走，
        这是调度的既有歧义处理策略，不是 bug。）
    - `batch_items`：整批原样传递，供画画/游戏等按条处理的分支使用
    - `viewer_identity_key` / `speaker`：chat_and_speak 据此写观众记忆
    """
    bodies = []
    for it in items:
        body = (it.get("display_text") or it.get("text") or "").strip()
        if body:
            bodies.append(body)
    user_input = "\n".join(bodies)

    last = items[-1]
    uid = int(last.get("uid") or 0)
    label = last.get("label") or last.get("speaker") or "观众"

    from memory.identity import bili_identity, legacy_identity
    identity_key = bili_identity(uid) if uid else legacy_identity(label)

    return dict(
        user_input=user_input,
        batch_items=items,
        memory_text=user_input,
        viewer_identity_key=identity_key,
        speaker=label,
    )


def _sync_rotation(scheduler, expected_speaker: str, log_fn) -> None:
    """按"本轮实际说话的人"把轮换游标对齐。

    ⚠️ 为什么需要：`conversation.chat_and_speak` 收尾时用的是
    `scheduler.advance()`（无脑游标 +1，conversation.py:1049）。当某一轮是被
    **@点名**选中、绕过了轮换时，游标就会漂移——典型症状是"搭档交接"连续两轮
    落到同一个人身上（实测：tou 讲完笑话后，又轮到 tou 接自己的话）。

    上游其实早就写好了正确的 API：`speaker_scheduler.advance_from(actual_speaker)`
    ——但它在整个仓库里**没有任何调用方**。这里在调用之后用它做一次绝对对齐
    （覆盖掉 advance() 造成的漂移）。
    """
    if not expected_speaker:
        return
    try:
        before = scheduler.next_speaker
        scheduler.advance_from(expected_speaker)
        after = scheduler.next_speaker
        if before != after:
            log_fn(f"  [轮换校正] 本轮实际说话者={expected_speaker}，"
                   f"下一位 {before} → {after}")
    except Exception as e:
        log_fn(f"  ! 轮换校正失败：{e}")


def run_one_turn(*, ctx, scheduler, log_fn, session_id: str) -> bool:
    """处理一轮观众输入。返回是否真的说了一轮。"""
    from conversation import chat_and_speak

    items = scheduler.pop_all_inputs(max_items=8)
    if not items:
        return False
    kwargs = _build_turn_inputs(items, session_id=session_id)
    log_fn(f"  → 本轮 {len(items)} 条输入，正文：{kwargs['user_input'][:40]}"
           f"（identity={kwargs['viewer_identity_key']}）")
    # 预先算一次"本轮会是谁说话"：与 chat_and_speak 内部用的是同一判据（纯函数，结果一致）
    expected = scheduler.pick_speaker(kwargs["user_input"])
    t0 = time.time()
    try:
        chat_and_speak(ctx, kwargs["user_input"], speaker=kwargs["speaker"],
                       input_type=items[-1].get("source", "danmaku"),
                       memory_text=kwargs["memory_text"],
                       viewer_identity_key=kwargs["viewer_identity_key"],
                       batch_items=kwargs["batch_items"])
    except Exception as e:
        import traceback
        log_fn(f"  ✗ 本轮失败（已兜住，直播继续）：{type(e).__name__}: {e}")
        log_fn(traceback.format_exc())
        return False
    _sync_rotation(scheduler, expected, log_fn)
    RUNTIME.touch_agent_speech(time.time())
    log_fn(f"  ← 本轮结束，耗时 {time.time() - t0:.2f}s")
    return True


def run_proactive(*, ctx, log_fn) -> bool:
    """空闲够久 → 主动说一句（也承担"游戏段落里主动解说"的职责）。"""
    from conversation import proactive_speak
    try:
        proactive_speak(ctx)
    except Exception as e:
        import traceback
        log_fn(f"  ✗ 主动发言失败（已兜住）：{type(e).__name__}: {e}")
        log_fn(traceback.format_exc())
        return False
    RUNTIME.touch_agent_speech(time.time())
    return True


# 搭档交接的触发词。**刻意不含任何角色名**：`pick_speaker()` 靠"文本里出现哪个
# 角色名"判 @点名，不含名字才会回落到轮换——而上一位说完时轮换游标已经推进
# （conversation.py:1049 的 `advance()`），所以这里自然选到"另一位"。
HANDOFF_PROMPT = (
    "（搭档刚说完一句，接一句你自己的反应：吐槽、附和、追问、拆台都行。"
    "只说一句短话；不要重复搭档说过的内容；不要念出「他说」「对方说」这种字眼。）"
)


def run_handoff(*, ctx, scheduler, log_fn) -> bool:
    """搭档交接：让另一位角色就搭档刚说的话接一句。

    这就是「两个 AI 互相接话」缺的那一环：

    - **内容侧**：上游的 `mirror_speech_to_partner()` 已把搭档那句以 `[X说] …`
      写进了本角色的 history（conversation.py:57-74），所以这里只要给一个触发词；
    - **路由**：提示词不含角色名 → 按轮换落到"另一位"（见 HANDOFF_PROMPT 注释）；
    - **speaker="未知"**：不给消息加 `[观众说]` 前缀、也不写观众记忆——
      这轮不是观众说的，写进记忆会污染长期记忆。
    """
    from conversation import chat_and_speak
    target = scheduler.pick_speaker(HANDOFF_PROMPT)   # 与内部判据一致
    try:
        chat_and_speak(ctx, HANDOFF_PROMPT, speaker="未知",
                       input_type="partner_handoff")
    except Exception as e:
        import traceback
        log_fn(f"  ✗ 搭档交接失败（已兜住，直播继续）：{type(e).__name__}: {e}")
        log_fn(traceback.format_exc())
        return False
    _sync_rotation(scheduler, target, log_fn)
    RUNTIME.touch_agent_speech(time.time())
    return True


def _pending_game():
    """当前是否有游戏在等决策（延迟导入，避免无游戏时也加载 games 包）。"""
    try:
        from launcher.games import has_pending_decision
        return has_pending_decision()
    except Exception:
        return None, None


def main_loop(*, ctx, scheduler, runtime, cfg, log_fn) -> int:
    """返回执行的轮数。退出条件：stop_event / --turns 到量 / 状态机进入 ENDING。"""
    turns = 0
    viewer_turns = 0
    idle_announced = False
    session_id = runtime.session_id
    log_fn("\n=== 进入主循环（Ctrl-C 或输入 /quit 下播）===")

    while not runtime.stop_event.is_set():
        # ★ 游戏决策优先：桥接在等快脑回话（决策窗口只有 20-35 秒），
        #   不能等"空闲超时"才跑——否则会直接掉进桥接的超时兜底策略。
        #   proactive_speak 内部会带上待决 game_request 并回填 result_event
        #   （conversation.py:1351-1361 / 1441-1448）。
        pending_name, _req = _pending_game()
        if pending_name and scheduler.queue_size() == 0:
            log_fn(f"  [游戏] {pending_name} 正在等待决策 → 立刻跑一轮")
            if run_proactive(ctx=ctx, log_fn=log_fn):
                turns += 1
            continue

        did = run_one_turn(ctx=ctx, scheduler=scheduler, log_fn=log_fn,
                           session_id=session_id)
        if did:
            turns += 1
            viewer_turns += 1
            idle_announced = False
            # 搭档交接：每 N 轮观众回应之后，让另一位就搭档刚说的那句接一句
            if cfg.handoff_every and viewer_turns % cfg.handoff_every == 0:
                log_fn("  [搭档交接] 让另一位就搭档刚说的那句接一句")
                if run_handoff(ctx=ctx, scheduler=scheduler, log_fn=log_fn):
                    turns += 1
        else:
            idle = runtime.idle_seconds(time.time())
            if cfg.idle_speak_after and idle >= cfg.idle_speak_after:
                if not idle_announced:
                    log_fn(f"  [空闲] 已静默 {idle:.0f}s → 主动说一句")
                if run_proactive(ctx=ctx, log_fn=log_fn):
                    turns += 1
            else:
                time.sleep(0.05)

        if cfg.turns and turns >= cfg.turns:
            log_fn(f"  [停止] 已跑够 {turns} 轮（--turns）")
            break

        # 自动化验收用：静默够久且没有待决决策 → 自动下播。
        # 没有它时，脚本输入喂完 + 关闭主动发言的组合会让主循环永久空转（实测踩过）。
        if cfg.stop_when_idle and not _pending_game()[0]:
            if runtime.idle_seconds(time.time()) >= cfg.stop_when_idle:
                log_fn(f"  [停止] 静默 {cfg.stop_when_idle:.0f}s 且无待决决策（--stop-when-idle）")
                break

    log_fn(f"\n=== 主循环结束，共 {turns} 轮 ===")
    return turns
