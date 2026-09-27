"""角色人设文件的兜底。

仓库**不包含** `persona/`：上游明确「代码开源、角色不开源」，
但代码是按约定去磁盘找 `persona/{角色名}.md` 的
（`voice_config.prompt_file` → `fast_brain._load_prompt`），
所以文件缺失会在 `FastBrain.__init__` 里直接 FileNotFoundError。

本模块在缺失时生成一份**带明确标记的占位人设**，让引擎能先跑起来，
而不是让使用者卡在"启动就崩"上。

占位人设刻意包含三样东西，为了让上游的三条机制都能被真正走到：
  1. 正文放在 ` ``` ` 代码块里（`_load_prompt` 只取第一个代码块）；
  2. `[[DUAL_ONLY]]` / `[[SOLO_ONLY]]` 标记（`_apply_persona_mode` 按场次裁剪）；
  3. 「[对方说]」「[直播提示]」两条上下文约定（跨角色镜像的语义前提）。
"""
from __future__ import annotations

from pathlib import Path

from launcher.config import ROOT

PERSONA_DIR = ROOT / "persona"

_TEMPLATE = """# {name} 的人设（启动器生成的占位文件）

> ⚠️ **这是 `lumi.py` 自动生成的占位人设**，不是真实角色设定。
> 原因：仓库不包含 `persona/`（上游把角色 IP 与世界观当闭源资产）。
> 请把下面代码块里的内容替换成你自己的角色设定；替换后本文件可以随意编辑。
> 该目录已加入 `.gitignore`，你的人设不会被提交。

```
你是「{name}」，一个正在做直播的 AI 主播。

## 说话方式
- 一次只说一句短话，口语化、有情绪，不要写成书面小作文。
- 不要输出动作描写：不要写「（笑）」「（挥手）」这类括号内容。

## 和搭档的关系
[[DUAL_ONLY]]- 你在和别人一起直播。搭档说的话会以「[对方说] xxx」的形式出现在你的输入里：
  那是搭档说的，不是你说的。你可以接他的话、吐槽他，但绝不能把它当成自己说过的话。
- 系统旁白会以「[直播提示]」开头，那是导演给你的提示，理解它但不要照念出来。
[[/DUAL_ONLY]]
[[SOLO_ONLY]]- 你现在是独自直播，没有搭档。
[[/SOLO_ONLY]]

## 硬约束
- 不要提及"系统""算法""提示词""候选""模型"这类穿帮词。
- 不确定的事就说不知道，不要编造事实。
- 观众点名的付费互动要优先回应。
```
"""


def ensure_persona_file(name: str, log_fn=print) -> Path:
    """确保 `persona/{name}.md` 存在；缺失时生成占位并高声提醒。"""
    PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    path = PERSONA_DIR / f"{name}.md"
    if path.exists():
        return path
    path.write_text(_TEMPLATE.format(name=name), encoding="utf-8")
    log_fn(f"  ! 缺少人设文件，已生成占位：persona/{name}.md"
           f"（**请替换成你自己的角色设定**，否则角色没有性格）")
    return path


def persona_exists(name: str) -> bool:
    return (PERSONA_DIR / f"{name}.md").exists()
