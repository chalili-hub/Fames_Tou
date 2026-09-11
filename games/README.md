# Game integrations

Each game lives with its bridge, decision logic and local assets. Commercial game
binaries are never included.

| Directory | Contents | Example entry point |
|---|---|---|
| `buckshot/` | Buckshot Roulette decision engine and TCP bridge | `python -m games.buckshot.bot` |
| `terraria/` | Terraria bridge, bundled focus-safe tModLoader mod, A* navigation and goal planner | `python -m games.terraria.bot --demo walk` |
| `kingdom_rush/` | Lua bridge, patcher and tower-defense AI | `python -m games.kingdom_rush.ai --dry-run` |
| `word_games/wordle/` | Wordle frontend and entropy solver | `python -m games.word_games.wordle.bot --no-llm` |
| `word_games/handle/` | Handle frontend and Chinese-idiom solver | `python -m games.word_games.handle.bridge` |

The word games share the isolated worker process in `word_games/solver_client.py`
and `word_games/solver_worker.py` so expensive entropy search cannot stall the host.

Terraria setup and the background-focus regression check are documented in
[`terraria/mod/README.md`](terraria/mod/README.md).
