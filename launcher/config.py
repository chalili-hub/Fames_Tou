"""参数解析、配置合并与启动体检。

`--doctor` 只做体检、不跑直播：把「缺 key / 缺人设文件 / 缺声卡 / 缺依赖」
在启动阶段一次性报清楚，而不是等跑到某一步才崩。
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 实时引擎需要的第三方依赖（协调内核不需要任何依赖）
ENGINE_DEPS = (
    "websockets", "openai", "dashscope", "dotenv", "numpy", "scipy",
    "pyaudiowpatch", "requests",
)

# 实时链路必需、但缺失时应"显式失败"的密钥（不是降级）
REQUIRED_KEYS_REALTIME = ("ARK_API_KEY_FAST", "VOLC_DIALOG_APP_ID", "VOLC_DIALOG_ACCESS_KEY")
# 文本链路只需要 LLM key
REQUIRED_KEYS_TEXT = ("ARK_API_KEY_FAST",)

OPTIONAL_KEYS = (
    "DASHSCOPE_API_KEY",                  # 独立 TTS / ASR
    "VOLC_WEBSEARCH_API_KEY", "VOLC_WEBSEARCH_BOT_ID",
    "CHARACTER_A_VOICE_ID", "CHARACTER_B_VOICE_ID",
)


@dataclass
class LauncherConfig:
    arch: str = "text"                      # text | realtime | duplex
    duplex_model: str = ""                  # duplex 会话模型（留空用协议默认 1.2.6.1）
    duplex_voice: str = ""                  # duplex 会话音色（留空用协议默认音色）
    duplex_wav_dir: str = ""                # 非空时把音频落盘（无声卡环境验证用）
    script_interval: float = 2.0            # --danmaku script 时每条弹幕的间隔秒数
    handoff_every: int = 0                  # >0：每 N 轮观众回应后让搭档接一句
    dump_history: str = ""                  # 非空则下播时把各角色 history 落盘（排查用）
    audio_device_keyword: str = ""          # 覆盖 voice_config 的声卡关键字（临时试设备用）
    characters: list = field(default_factory=lambda: ["fames", "tou"])
    model: str = ""                         # 空 = 用 fast_brain 默认
    cooldown: float = 0.0                   # ★ 0 = 关闭（与 arbiter 默认一致）
    danmaku: str = "stdin"                  # stdin | none | bili
    game: str = "none"                      # none | kr | terraria | wordle | handle | buckshot
    mock_game: bool = False                 # 用模拟游戏端替代真实游戏（验证接线用）
    enable_tts: bool = True
    enable_audio: bool = True
    enable_drawing: bool = False
    dry_run: bool = False
    doctor: bool = False
    probe: bool = False                     # 只探针：验证端到端（SC2.0）凭证是否可用
    probe_session: bool = False             # 探针是否进一步验证 model 版本与音色
    probe_voice: str = ""                   # 探针用的音色 ID（留空取 voice_config）
    probe_app_id: str = ""                  # 探针临时指定 App ID（不改 .env，便于试值）
    probe_access_key: str = ""              # 探针临时指定 Access Key（同上）
    turns: int = 0                          # >0 时跑够轮数就退出（干跑/自测用）
    drain_seconds: float = 0.0              # 下播前排空在途决策/音频的等待秒数
    stop_when_idle: float = 0.0             # >0：静默该秒数且无待决决策就自动下播（自动化用）
    idle_speak_after: float = 25.0          # 沉默多少秒触发主动说话（0 = 关闭）
    session_id: str = ""
    db_path: str = "memory/memory.db"
    report_path: str = ""                   # 非空则把复盘报告写成 json
    log_level: str = "INFO"
    no_memory: bool = False

    @property
    def multi_character(self) -> bool:
        return len(self.characters) > 1


def parse_args(argv=None) -> LauncherConfig:
    p = argparse.ArgumentParser(
        prog="lumi.py",
        description="双 AI 角色实时共播引擎 · 启动器（装配层）",
    )
    p.add_argument("--arch", choices=("text", "realtime", "duplex"), default="text",
                   help="运行架构：text=独立 LLM+TTS；realtime=旧版端到端（二进制协议）；"
                        "duplex=新版端到端实时语音（JSON 事件协议，推荐）")
    p.add_argument("--duplex-model", default="", help="duplex 会话模型（默认 1.2.6.1）")
    p.add_argument("--duplex-voice", default="", help="duplex 会话音色（默认协议内置音色）")
    p.add_argument("--duplex-wav-dir", default="",
                   help="把 duplex 音频落盘到该目录（无声卡时验证链路用）")
    p.add_argument("--script-interval", type=float, default=2.0,
                   help="--danmaku script 时每条弹幕的间隔秒数；"
                        "设大一点（如 12）可让每轮只回应一条，便于录演示")
    p.add_argument("--handoff-every", type=int, default=0,
                   help="★ 搭档交接：每 N 轮观众回应后，让另一位就搭档刚说的那句接一句"
                        "（0=关闭；设为 1 就是「你一句我一句」的双主播效果）")
    p.add_argument("--dump-history", default="",
                   help="下播时把各角色的对话 history 写到该目录（排查镜像/上下文用）")
    p.add_argument("--audio-device-keyword", default="",
                   help="临时覆盖所有角色的声卡关键字（例：--audio-device-keyword Realtek "
                        "就先用系统扬声器出声，不必装虚拟声卡）")
    p.add_argument("--characters", default="fames,tou",
                   help="角色列表，逗号分隔（单角色也能跑，例：fames）")
    p.add_argument("--model", default="",
                   help="快脑模型：可用注册表 key（2.0-lite / 2.0-mini / 1.6-flash …），"
                        "也可直接给方舟 model id 或推理接入点 ep-xxxx；"
                        "留空则读环境变量 ARK_MODEL_ID，再留空用代码默认")
    p.add_argument("--cooldown", type=float, default=0.0,
                   help="★ 连麦冷却秒数（0=关闭，与仲裁器默认一致）")
    p.add_argument("--danmaku", choices=("stdin", "script", "none", "bili"), default="stdin",
                   help="观众输入源；stdin=命令行模拟（零依赖）；script=内置脚本（自动验收/录演示）")
    p.add_argument("--game", choices=("none", "kr", "terraria", "wordle", "handle", "buckshot"),
                   default="none", help="是否拉起游戏段落桥接")
    p.add_argument("--mock-game", action="store_true",
                   help="用本地模拟游戏端替代真实游戏（验证桥接接线用，不用开游戏）")
    p.add_argument("--no-tts", dest="enable_tts", action="store_false",
                   help="关声：只走纯文本生成（调试主循环用）")
    p.add_argument("--no-audio", dest="enable_audio", action="store_false",
                   help="不打开任何音频设备（没有声卡时用）")
    p.add_argument("--enable-drawing", action="store_true",
                   help="画画段落（上游未开源，本层为占位实现）")
    p.add_argument("--dry-run", action="store_true",
                   help="假大脑 + 假声卡，零依赖跑一整场（不需要任何 key）")
    p.add_argument("--doctor", action="store_true", help="只做启动体检，不跑直播")
    p.add_argument("--probe", action="store_true",
                   help="只探针：验证端到端（SC2.0）凭证能否握手成功，不跑直播")
    p.add_argument("--probe-session", action="store_true",
                   help="探针再进一步验证 model 版本与音色（会真的调 StartSession）")
    p.add_argument("--probe-voice", default="", help="探针用的音色 ID（默认取 voice_config）")
    p.add_argument("--probe-app-id", default="",
                   help="探针临时指定 App ID（覆盖 .env，便于快速试值）")
    p.add_argument("--probe-access-key", default="",
                   help="探针临时指定 Access Key（覆盖 .env，便于快速试值）")
    p.add_argument("--turns", type=int, default=0, help="跑够 N 轮后自动下播（干跑/自测用）")
    p.add_argument("--drain-seconds", type=float, default=0.0,
                   help="下播前排空在途决策/音频的等待秒数（有游戏段落时建议 1.5~3）")
    p.add_argument("--stop-when-idle", type=float, default=0.0,
                   help="静默 N 秒且没有待决游戏决策就自动下播（0=不限；自动化验收用）")
    p.add_argument("--idle-speak-after", type=float, default=25.0,
                   help="沉默多少秒后主动说话（0=关闭）")
    p.add_argument("--session-id", default="", help="本场 session id（留空自动生成）")
    p.add_argument("--db-path", default="memory/memory.db", help="记忆库路径")
    p.add_argument("--report", dest="report_path", default="", help="复盘报告落盘路径（json）")
    p.add_argument("--log-level", default="INFO",
                   choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    p.add_argument("--no-memory", action="store_true", help="不启用长期记忆（调试用）")
    a = p.parse_args(argv)

    chars = [c.strip() for c in (a.characters or "").split(",") if c.strip()]
    if not chars:
        p.error("--characters 不能为空")
    # 有游戏段落时给一个默认排空窗口：桥接在决策后会 sleep 1s 才下发命令，
    # 立刻下播会把这一手决策砍掉（实测踩过）。
    drain = a.drain_seconds
    if drain <= 0 and a.game != "none":
        drain = 1.5
    if a.dry_run:
        # 干跑必须零依赖：强制关掉音频与真实 TTS，输入由 ScriptedSource 脚本喂
        a.enable_audio = False
        a.enable_tts = False
        a.danmaku = "none"
        if a.turns <= 0:
            a.turns = 6
        # 干跑必须有确定的终止条件：否则脚本喂完后无输入、无待决决策时会一直空转
        if a.stop_when_idle <= 0:
            a.stop_when_idle = 5.0
    return LauncherConfig(
        arch=a.arch, characters=chars,
        # 模型选择顺序：--model → 环境变量 ARK_MODEL_ID → 代码默认
        model=a.model or os.environ.get("ARK_MODEL_ID", "") or "",
        cooldown=a.cooldown,
        duplex_model=a.duplex_model, duplex_voice=a.duplex_voice,
        duplex_wav_dir=a.duplex_wav_dir, script_interval=a.script_interval,
        handoff_every=a.handoff_every, dump_history=a.dump_history,
        audio_device_keyword=a.audio_device_keyword,
        danmaku=a.danmaku, game=a.game, enable_tts=a.enable_tts,
        mock_game=a.mock_game,
        enable_audio=a.enable_audio, enable_drawing=a.enable_drawing,
        dry_run=a.dry_run, doctor=a.doctor, turns=a.turns,
        probe=a.probe, probe_session=a.probe_session, probe_voice=a.probe_voice,
        probe_app_id=a.probe_app_id, probe_access_key=a.probe_access_key,
        drain_seconds=drain, stop_when_idle=a.stop_when_idle,
        idle_speak_after=a.idle_speak_after, session_id=a.session_id,
        db_path=a.db_path, report_path=a.report_path, log_level=a.log_level,
        no_memory=a.no_memory,
    )


def load_env(*, quiet: bool = False) -> dict:
    """加载 .env 与可选的项目本地 config.py（被 gitignore 的运行时配置）。"""
    loaded = {"dotenv": False, "config_py": False}
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        loaded["dotenv"] = True
    except Exception:
        if not quiet:
            print("[配置] 未安装 python-dotenv，跳过 .env 加载")
    if (ROOT / "config.py").exists():
        sys.path.insert(0, str(ROOT))
        loaded["config_py"] = True
    return loaded


def persona_files(cfg: LauncherConfig) -> dict:
    """角色人设文件路径。仓库不包含 persona/（上游把角色当闭源资产）。"""
    out = {}
    for name in cfg.characters:
        out[name] = {
            "text": ROOT / "persona" / f"{name}.md",
            "realtime": ROOT / "persona" / f"{name}_realtime.md",
        }
    return out


def _voice_id_env_keys(name: str) -> str:
    """与 voice_registry 的解析规则保持一致：voice_name → PREFIX_VOICE_ID。"""
    return name.upper().replace("-", "_")


def run_doctor(cfg: LauncherConfig) -> int:
    """启动体检。返回 0=可以跑，1=有阻断项（仅提示，不强制退出）。"""
    print("=" * 68)
    print("启动体检 (--doctor)")
    print("=" * 68)
    blockers, warnings = [], []

    # 1) Python 版本（仓库用了 PEP 585 泛型，3.9 以下 import 即失败）
    py_ok = sys.version_info >= (3, 9)
    print(f"[Python] {sys.version.split()[0]}  {'OK' if py_ok else '不满足 >=3.9'}")
    if not py_ok:
        blockers.append("Python < 3.9：event_bus/state_machine 等模块 import 会直接失败")

    # 2) 依赖
    print("\n[依赖]")
    for mod in ENGINE_DEPS:
        ok = importlib.util.find_spec(mod) is not None
        print(f"  {mod:<16} {'OK' if ok else '缺失'}")
        if not ok and not cfg.dry_run:
            warnings.append(f"缺少依赖 {mod}（pip install -r requirements.txt）")

    # 3) 密钥
    print("\n[密钥]")
    required = REQUIRED_KEYS_REALTIME if cfg.arch == "realtime" else REQUIRED_KEYS_TEXT
    for k in required:
        ok = bool(os.environ.get(k))
        print(f"  {k:<26} {'OK' if ok else '未设置'}  (必需)")
        if not ok and not cfg.dry_run:
            blockers.append(f"缺少必需密钥 {k}")
    for k in OPTIONAL_KEYS:
        ok = bool(os.environ.get(k))
        print(f"  {k:<26} {'OK' if ok else '未设置'}  (可选)")

    # 4) 人设文件
    print("\n[人设]")
    for name, paths in persona_files(cfg).items():
        for kind, path in paths.items():
            ok = path.exists()
            tag = "" if ok else ("（干跑可缺省）" if cfg.dry_run else "→ 角色会失去性格")
            print(f"  {name}/{kind:<9} {'OK' if ok else '缺失'}  {path.name} {tag}")
            if not ok and not cfg.dry_run:
                warnings.append(f"缺人设文件 {path.relative_to(ROOT)}（代码会去找它）")

    # 5) 记忆库
    print("\n[记忆]")
    db = ROOT / cfg.db_path
    print(f"  数据库路径 {cfg.db_path}  存在={db.exists()}  启用={not cfg.no_memory}")
    if sys.version_info >= (3, 9):
        try:
            from memory.runtime import MemoryRuntime          # noqa: F401
            print("  memory 模块导入 OK")
        except Exception as e:
            warnings.append(f"memory 模块导入失败：{e}")
    else:
        print("  memory 模块导入 跳过（Python 版本不足）")

    # 6) 协调内核（零依赖，必须可导入）
    print("\n[协调内核]")
    if sys.version_info >= (3, 9):
        try:
            from event_bus import EventBus                    # noqa: F401
            from state_machine import StateMachine            # noqa: F401
            from speaker_scheduler import SpeakerScheduler    # noqa: F401
            from speech_output_arbiter import SpeechOutputArbiter  # noqa: F401
            from stream_analytics import StreamAnalytics      # noqa: F401
            print("  bus / state / scheduler / arbiter / analytics 导入 OK")
        except Exception as e:
            blockers.append(f"协调内核导入失败：{e}")
            print(f"  导入失败：{e}")
    else:
        print("  跳过（Python 版本不足）")

    # 7) 凭证实测（会真的发请求；缺 key / 不可用都会说清怎么处置）
    print("\n[凭证实测]")
    if sys.version_info >= (3, 9):
        try:
            from launcher.probe import probe_ark_key
            probe_ark_key(log_fn=print)
        except Exception as e:
            print(f"  · ARK 实测跳过：{e}")
        print("  · 语音（duplex）实测：python -m launcher.duplex_client --text 测试")
        print("    或：python lumi.py --probe")
    else:
        print("  跳过（Python 版本不足）")

    # 8) 断言式自检：仲裁器是否真被配置上总线（本层最容易漏的一步）
    print("\n[装配自检]")
    print("  见 tests/test_launcher_assembly.py（python -m unittest discover -s tests）")

    print("\n" + "=" * 68)
    if blockers:
        print(f"阻断项 {len(blockers)} 个：")
        for b in blockers:
            print(f"  ✗ {b}")
    if warnings:
        print(f"提醒 {len(warnings)} 个：")
        for w in warnings:
            print(f"  ! {w}")
    if not blockers and not warnings:
        print("体检通过：可以开播。")
    print("=" * 68)
    return 1 if blockers else 0
