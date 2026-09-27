"""生命周期：启动自检、优雅退出、记忆蒸馏、复盘报告落盘。

退出顺序是有讲究的（顺序错了会出现"退出时还在说话"或"音频被截断"）：
    停输入 → 停生成/播放 → 关会话 → 蒸馏记忆 → 出报告 → 关子进程
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from launcher.registry import RUNTIME


def startup_check(cfg, log_fn) -> list:
    """启动期体检（比 --doctor 轻量）：只报会影响本场运行的问题。"""
    problems = []
    if not cfg.dry_run:
        import os
        if not os.environ.get("ARK_API_KEY_FAST"):
            problems.append("缺少 ARK_API_KEY_FAST：快脑无法调用，本场只会走干跑占位台词")
        if cfg.arch == "realtime" and not os.environ.get("VOLC_DIALOG_ACCESS_KEY"):
            problems.append("端到端架构缺少 VOLC_DIALOG_APP_ID/ACCESS_KEY")
    from launcher.config import persona_files
    for name, paths in persona_files(cfg).items():
        if not paths["text"].exists() and not cfg.dry_run:
            problems.append(f"缺人设文件 persona/{name}.md（角色会失去性格）")
    for p in problems:
        log_fn(f"  ! {p}")
    return problems


def finalize_memory(eng, log_fn) -> dict:
    """会话结束 → 把本场记忆蒸馏入库（同步路径，带确定性上舰兜底）。"""
    if eng.memory is None:
        log_fn("[记忆] 未启用，跳过蒸馏")
        return {"skipped": True, "reason": "memory_disabled"}
    if eng.cfg.dry_run:
        log_fn("[记忆] 干跑模式跳过蒸馏（蒸馏需要 LLM key；库里已写入原始语料）")
        return {"skipped": True, "reason": "dry_run"}
    try:
        from memory.finalize import finalize_session_memory
        stats = finalize_session_memory(eng.memory.storage, RUNTIME.session_id, log_fn=log_fn)
        log_fn(f"[记忆] 蒸馏完成：{stats}")
        return stats
    except Exception as e:
        log_fn(f"[记忆] 蒸馏失败（不影响下播）：{type(e).__name__}: {e}")
        return {"skipped": True, "reason": str(e)}


def write_report(eng, path: str | None, log_fn) -> str:
    """复盘报告：stdout 文本 + 可选 json 落盘。"""
    if eng.analytics is None:
        return ""
    try:
        eng.analytics.detach()          # 停止计数，避免退出阶段的噪声事件混进报告
    except Exception:
        pass
    text = eng.analytics.report()
    if path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = eng.analytics.summary()
        payload["session_id"] = RUNTIME.session_id
        payload["turns_played"] = eng.turns_played
        payload["turn_log"] = RUNTIME.turns()
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log_fn(f"[复盘] 结构化报告已写入 {out}")
    return text


def dump_histories(eng, out_dir: str, log_fn) -> None:
    """把各角色的对话 history 落盘（排查镜像/上下文用）。

    为什么需要：搭档镜像是否真的生效、上下文有没有串，光看日志的"台词"是推不出来的
    （要看到 `[fames说] …` 这类条目落进了谁的 history）。运行期加 `--dump-history 目录` 即可。
    """
    ctx = getattr(eng, "context", None)
    if ctx is None or not out_dir:
        return
    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, brain in (ctx.fast_brains or {}).items():
            path = out / f"history_{name}.txt"
            lines = []
            for i, msg in enumerate(getattr(brain, "history", []) or []):
                role = msg.get("role", "?") if isinstance(msg, dict) else "?"
                content = (msg.get("content", "") if isinstance(msg, dict) else str(msg))
                lines.append(f"[{i:02d}] {role}: {content}")
            path.write_text("\n".join(lines), encoding="utf-8")
            mirrored = sum(1 for ln in lines if "[fames说]" in ln or "[tou说]" in ln)
            log_fn(f"[排查] {name} 的 history 已落盘：{path}"
                   f"（{len(lines)} 条，其中搭档镜像 {mirrored} 条）")
    except Exception as e:
        log_fn(f"  ! history 落盘失败：{e}")


def shutdown(eng, log_fn) -> None:
    log_fn("\n=== 下播（按序收尾）===")

    # 0) 排空窗口：让在途的决策/音频跑完再收摊。
    #    实测教训：恶魔轮盘桥接在决策后会 sleep 1s 才把命令下发给游戏端，
    #    若立刻下播，这一手决策会被直接砍掉（游戏端永远收不到）。
    drain = float(getattr(eng.cfg, "drain_seconds", 0) or 0)
    if drain > 0:
        import time as _t
        from launcher import games as _G
        t0 = _t.time()
        log_fn(f"  0/6 排空在途决策/音频（最多 {drain:.1f}s）")
        # ⚠️ 不能只用"是否还有 pending decision"当判据：
        #    桥接在**决策完成时就清掉 pending**（bridge.py:689-690），
        #    之后还要 sleep 1s 才把命令下发给游戏端——只等 pending 会漏掉这一段，
        #    表现为"下播把这一手决策砍掉、游戏端永远收不到命令"（实测踩过）。
        #    所以这里按窗口等满，并把 pending 状态打出来方便排查。
        while _t.time() - t0 < drain:
            name, _req = _G.has_pending_decision()
            _t.sleep(0.1)
            if _t.time() - t0 >= drain:
                break
            if name is None and _t.time() - t0 > max(1.2, drain * 0.6):
                break   # 已无待决决策且过了"下发缓冲"，可以提前结束
        log_fn(f"      排空结束（等待 {_t.time() - t0:.1f}s）")

    # 1) 停输入
    for src in (eng.source, eng.mic):
        try:
            if src is not None:
                src.stop()
        except Exception as e:
            log_fn(f"  ! 停输入失败：{e}")
    log_fn("  1/6 输入已停")

    # 2) 停生成 / 播放
    if eng.asr is not None:
        try:
            eng.asr.stop()
        except Exception as e:
            log_fn(f"  ! 停 ASR 失败：{e}")
    if eng.realtime is not None:
        try:
            eng.realtime.stop_mic_pump()
            eng.realtime.stop_audio_output()
        except Exception as e:
            log_fn(f"  ! 停音频输出失败：{e}")
    log_fn("  2/6 语音输入输出已停")

    # 3) 关会话
    if getattr(eng, "duplex", None) is not None:
        try:
            eng.duplex.close()
        except Exception as e:
            log_fn(f"  ! 关 duplex 会话失败：{e}")
    if eng.realtime is not None:
        try:
            eng.realtime.close_session()
        except Exception as e:
            log_fn(f"  ! 关会话失败：{e}")
    log_fn("  3/6 会话已关")

    # 4) 状态机收尾 + 游戏桥接
    try:
        from state_machine import State
        if eng.state is not None and eng.state.state.value != "IDLE":
            # ★ 活动态（PLAYING_* / DRAWING）**不能直接**转 ENDING：
            #   状态转移表里只有 PLAYING_* → TRANSITIONING 这一条路
            #   （state_machine.py:34-51）。这里正好用上 is_activity()——
            #   它原本是个没有调用方的 API。
            if eng.state.is_activity():
                eng.state.transition_to(State.TRANSITIONING, metadata={"to": "ENDING"})
            if eng.state.state.value != "ENDING":
                eng.state.transition_to(State.ENDING)
            eng.state.transition_to(State.IDLE)
        log_fn("  4/6 状态机已回到 IDLE")
    except Exception as e:
        log_fn(f"  ! 状态机收尾失败：{e}")

    from launcher import games as G
    G.stop_games(eng.games, log_fn)

    # 5) 记忆蒸馏
    finalize_memory(eng, log_fn)
    log_fn("  5/6 记忆处理完成")

    # 5.5) 排查用：把各角色 history 落盘（含搭档镜像条目）
    if getattr(eng.cfg, "dump_history", ""):
        dump_histories(eng, eng.cfg.dump_history, log_fn)

    # 6) 复盘报告 + 子进程
    text = write_report(eng, eng.cfg.report_path or None, log_fn)
    if text:
        print("\n" + text)
    try:
        from games.word_games import solver_client
        solver_client.shutdown()
    except Exception:
        pass
    try:
        if eng.pa is not None:
            eng.pa.terminate()
    except Exception:
        pass
    log_fn("  6/6 复盘报告已输出，资源已释放")
    log_fn("=== 下播完成 ===")
