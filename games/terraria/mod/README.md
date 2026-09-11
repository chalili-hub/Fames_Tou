# LumiBridge tModLoader mod

This directory contains the source for the Terraria-side TCP bridge used by
`games.terraria.bot`. The bot builds this bundled source before launching
tModLoader, so a fresh clone no longer depends on an unpublished local
`ModSources/LumiBridge` directory.

## Why the mod is required

Terraria clears normal player controls when its window loses focus. LumiBridge
reapplies AI control flags in `PostUpdateRunSpeeds()`, after that focus-dependent
reset and immediately before movement. It also disables inactive sleeping and
auto-pause. This lets the AI client keep following or walking while another
Terraria client is focused.

## Run

Install tModLoader and the Python dependencies first. If tModLoader is installed
outside the default path, set its directory in PowerShell:

```powershell
$env:TMODLOADER_PATH = "C:\Program Files (x86)\Steam\steamapps\common\tModLoader"
python -m games.terraria.bot --demo walk
```

The default command builds the source in this directory. If tModLoader was
already running, fully exit and restart it after the build so version `5.3` is
loaded. The bot prints `Mod 版本: 5.3 ✓` before enabling automatic control.

Use `--skip-build` only when the current LumiBridge mod is already installed.
Use `LUMIBRIDGE_SOURCE_PATH` only when intentionally testing a different source
checkout.

## Focus regression check

1. Start the AI client and issue a continuous action such as walk or follow.
2. Click the player-controlled Terraria client and leave the AI client unfocused.
3. Confirm the AI character continues moving and state ticks continue updating.

LumiBridge listens on `127.0.0.1:9877` by default. With two local tModLoader
clients, start the AI client first (or enable LumiBridge only there) so it owns
the bridge port.

Commercial game files and compiled `.tmod` artifacts are intentionally excluded.
