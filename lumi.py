"""lumi.py — 双 AI 实时共播引擎的启动器（装配层）。

上游仓库**不包含**这一层：它把 conversation.py / fast_brain.py / lumi_tts.py
等模块从原来的单体 lumi.py 里拆了出来，但把编排入口本身留在了闭源侧。
结果是仓库里留下大量"有实现、没调用方"的函数（详见 docs/LAUNCHER.md 的清单）。
本文件把那些零件接成一场能跑的直播。

它只做四件事：**参数 → 装配 → 跑主循环 → 按序收尾**。
其余职责都在 launcher/ 包里（见 launcher/__init__.py 的分工表）。

用法：
    python lumi.py --doctor                    # 只体检：key / 人设 / 声卡 / 依赖
    python lumi.py --dry-run                   # 零依赖干跑一整场（不需要任何 key）
    python lumi.py --danmaku stdin            # 文本架构，命令行喂弹幕
    python lumi.py --arch realtime            # 端到端语音链路（需要 SC2.0 凭证）
    python lumi.py --cooldown 3 --report out.json
"""
from __future__ import annotations

import logging
import sys

from launcher.config import load_env, parse_args, run_doctor
from launcher.registry import RUNTIME


def _setup_logging(level: str):
    """全项目统一的日志配置。

    上游各模块只用 `logging.getLogger(...)`，但仓库里没有 basicConfig/
    dictConfig——日志格式与级别实际由不可见的上层决定。本层把它补上。
    """
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _preflight_keys(cfg, log) -> int:
    """重模块导入之前的密钥预检。

    为什么必须有这一步：`fast_brain.py:28` 在**模块导入期**就构造
    `OpenAI(api_key=os.getenv("ARK_API_KEY_FAST"), ...)`，缺 key 时新版 SDK 直接抛
    `OpenAIError`——于是用户看到的是一段来自 openai 内部的堆栈，而不是"缺哪个 key、去哪申请"。
    这里提前拦住，把失败信息换成可执行的指引。
    """
    import os
    required = ["ARK_API_KEY_FAST"]
    if cfg.arch == "text":
        # 文本架构的流式 TTS / ASR 走 DashScope（CosyVoice / fun-asr）
        required.append("DASHSCOPE_API_KEY")
    elif cfg.arch == "realtime":
        required += ["VOLC_DIALOG_APP_ID", "VOLC_DIALOG_ACCESS_KEY"]
    # duplex：只需要一个 API Key（在 .env 里有两个可用名字，故单独校验）

    missing = [k for k in required if not (os.environ.get(k) or "").strip()]
    if cfg.arch == "duplex":
        from launcher.duplex_client import resolve_api_key
        if not resolve_api_key():
            missing.append("VOLC_DUPLEX_API_KEY（或 VOLC_DIALOG_ACCESS_KEY）")

    if not missing:
        # ★ 上游 `fast_brain.py:28` 与 `:32` **在导入期**各构造一个 OpenAI 客户端
        #   （方舟 + DashScope）。于是 `.env.example` 标注为"可选"的 DASHSCOPE_API_KEY
        #   实际上是"能 import 就必须非空"。非文本架构根本用不到 DashScope，
        #   这里补一个占位值以免用户被无关的 OpenAIError 拦住（真要用时会由服务端 401 反映出来）。
        if cfg.arch != "text" and not (os.environ.get("DASHSCOPE_API_KEY") or "").strip():
            os.environ["DASHSCOPE_API_KEY"] = "placeholder-not-used-in-this-arch"
            log("  · 本架构不需要 DashScope：为满足上游导入期构造客户端的写法，已填占位值（不会调用）")
        return 0
    log("=" * 68)
    log(f"✗ 缺少必需密钥：{', '.join(missing)}")
    log("=" * 68)
    log("  说明（按架构区分）：")
    log("    · ARK_API_KEY_FAST —— 快脑（生成台词），三种架构都需要。")
    log("    · DASHSCOPE_API_KEY —— 仅 text 架构需要（CosyVoice TTS / fun-asr）。")
    log("    · VOLC_DUPLEX_API_KEY —— duplex 架构需要（新版端到端实时语音，一个 Key 就够）。")
    log("    · VOLC_DIALOG_APP_ID / _ACCESS_KEY —— 仅旧版 realtime 架构需要。")
    log("  处置：")
    log("    1) 把它们写进仓库根目录的 .env（模板见 .env.example）；")
    log("    2) 跑 `python lumi.py --doctor` 看完整清单（含人设/声卡/数据库）；")
    log("    3) 语音凭证的申请与验证见 docs/CREDENTIALS.md；")
    log("       duplex 可直接用 `python -m launcher.duplex_client --text 测试` 验一条。")
    log("=" * 68)
    return 1


def main(argv=None) -> int:
    cfg = parse_args(argv)
    _setup_logging(cfg.log_level)
    log = lambda msg: print(msg, flush=True)          # noqa: E731

    env = load_env()
    if cfg.doctor:
        return run_doctor(cfg)

    if cfg.probe:
        from launcher.probe import probe_credentials
        return probe_credentials(timeout=12.0, with_session=cfg.probe_session,
                                 voice_id=cfg.probe_voice or None,
                                 app_id=cfg.probe_app_id or None,
                                 access_key=cfg.probe_access_key or None,
                                 log_fn=log)

    if cfg.dry_run:
        from launcher.dry_run import run_dry
        print(f"[启动] 干跑模式（.env 加载={env['dotenv']}）")
        run_dry(cfg, log_fn=log)
        return 0

    # ★ 必须在导入 launcher.assemble 之前：那条链路会 import fast_brain，
    #   而 fast_brain 在导入期构造 OpenAI 客户端（缺 key 直接抛 OpenAIError）。
    if _preflight_keys(cfg, log) != 0:
        return 1

    from launcher import lifecycle
    from launcher.assemble import build_engine
    from launcher.loops import main_loop

    print("=" * 68)
    print(f"启动：架构={cfg.arch} 角色={cfg.characters} 冷却={cfg.cooldown}s "
          f"输入源={cfg.danmaku} TTS={cfg.enable_tts} 音频={cfg.enable_audio}")
    print("=" * 68)

    eng = build_engine(cfg, log_fn=log)
    lifecycle.startup_check(cfg, log)

    try:
        eng.source.start()
        eng.turns_played = main_loop(ctx=eng.context, scheduler=eng.scheduler,
                                     runtime=RUNTIME, cfg=cfg, log_fn=log)
    except KeyboardInterrupt:
        log("\n[退出] 收到 Ctrl-C")
    except Exception as e:
        import traceback
        log(f"\n[致命] 主循环异常退出：{type(e).__name__}: {e}")
        log(traceback.format_exc())
    finally:
        RUNTIME.stop_event.set()
        lifecycle.shutdown(eng, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
