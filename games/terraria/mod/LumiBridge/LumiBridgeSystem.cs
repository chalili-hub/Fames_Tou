using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using Microsoft.Xna.Framework;
using Terraria;
using Terraria.ID;
using Terraria.Localization;
using Terraria.ModLoader;

namespace LumiBridge
{
	/// <summary>
	/// TCP Bridge Server - runs alongside the game, pushes state and receives commands.
	/// Architecture mirrors BridgeMod.gd from Buckshot Roulette.
	/// </summary>
	public class LumiBridgeSystem : ModSystem
	{
		private TcpListener _listener;
		private TcpClient _client;
		private NetworkStream _stream;
		private Thread _listenThread;
		private readonly object _lock = new();
		private bool _running;

		// Command queue (written by network thread, consumed by game thread)
		private readonly Queue<string> _commandQueue = new();

		// State tracking for change detection
		private int _lastHealth = -1;
		private float _lastPosX = -1;
		private float _lastPosY = -1;
		private int _tickCounter;

		// Static reference for combat events from GlobalNPC/ModPlayer
		private static LumiBridgeSystem _instance;

		// Control flags (set by commands, consumed by ModPlayer)
		public static bool ControlLeft;
		public static bool ControlRight;
		public static bool ControlUp;
		public static bool ControlDown;
		public static bool ControlJump;
		public static bool ControlUseItem;
		public static bool ControlQuickHeal;
		public static bool AutoMode;
		public static int TargetTileX = -1;
		public static int TargetTileY = -1;
		public static string PendingAction = "";

		// Navigator state
		private static Navigator _navigator = new Navigator();

		// Mod version — bump this when C# code changes, Python side checks on connect
		public const string MOD_VERSION = "5.3";

		// hold_use_item: countdown frames (>0 means hold use is active)
		public static int HoldUseFrames = 0;

		private const int PORT = 9877;
		private const int STATE_PUSH_INTERVAL_NORMAL = 30; // 0.5s when idle
		private const int STATE_PUSH_INTERVAL_AUTO = 6;    // 0.1s when auto mode

		public override void OnModLoad()
		{
			_instance = this;

			// 独立服务器上没有本地玩家，不启动 TCP 监听
			if (Main.dedServ)
			{
				Mod.Logger.Info("LumiBridge: 独立服务器模式，跳过 TCP 监听");
				return;
			}

			// Keep game running when window is not focused
			Main.instance.InactiveSleepTime = TimeSpan.Zero;

			_running = true;
			_listenThread = new Thread(ListenLoop)
			{
				IsBackground = true,
				Name = "LumiBridge-TCP"
			};
			_listenThread.Start();
			Mod.Logger.Info($"LumiBridge TCP server starting on port {PORT}");
		}

		public override void OnModUnload()
		{
			_instance = null;
			_running = false;
			try { _client?.Close(); } catch { }
			try { _listener?.Stop(); } catch { }
			ResetControls();
			Mod.Logger.Info("LumiBridge TCP server stopped");
		}

		private void ListenLoop()
		{
			try
			{
				_listener = new TcpListener(IPAddress.Loopback, PORT);
				_listener.Start();

				while (_running)
				{
					if (_listener.Pending())
					{
						lock (_lock)
						{
							try { _client?.Close(); } catch { }
							_client = _listener.AcceptTcpClient();
							_client.NoDelay = true;
							_stream = _client.GetStream();
						}
						SendEvent("connected", new { message = "LumiBridge connected" });
						ReadLoop();
					}
					Thread.Sleep(100);
				}
			}
			catch (Exception ex)
			{
				if (_running)
					Mod.Logger.Error($"LumiBridge listen error: {ex.Message}");
			}
		}

		private void ReadLoop()
		{
			try
			{
				var reader = new StreamReader(_stream, Encoding.UTF8);
				while (_running && _client.Connected)
				{
					string line = reader.ReadLine();
					if (line == null) break;
					lock (_commandQueue)
					{
						_commandQueue.Enqueue(line);
					}
				}
			}
			catch (Exception ex)
			{
				if (_running)
					Mod.Logger.Warn($"LumiBridge client disconnected: {ex.Message}");
			}
		}

		public override void PostUpdateEverything()
		{
			// Force game to run in background every frame
			Main.instance.InactiveSleepTime = TimeSpan.Zero;
			Main.autoPause = false;

			// Process commands from network thread
			ProcessCommands();

			// Update navigator (path execution)
			if (_navigator.Active && AutoMode)
				_navigator.Update(this);

			// Push state periodically (faster when auto mode)
			_tickCounter++;
			int interval = AutoMode ? STATE_PUSH_INTERVAL_AUTO : STATE_PUSH_INTERVAL_NORMAL;
			if (_tickCounter >= interval)
			{
				_tickCounter = 0;
				PushState();
			}
		}

		private void ProcessCommands()
		{
			lock (_commandQueue)
			{
				while (_commandQueue.Count > 0)
				{
					string raw = _commandQueue.Dequeue();
					try
					{
						HandleCommand(raw);
					}
					catch (Exception ex)
					{
						Mod.Logger.Error($"LumiBridge command error: {ex.Message}");
					}
				}
			}
		}

		private void HandleCommand(string raw)
		{
			using var doc = JsonDocument.Parse(raw);
			var root = doc.RootElement;
			string cmd = root.GetProperty("cmd").GetString();

			switch (cmd)
			{
				case "move":
				{
					ResetMovement();
					// Support single direction or array of directions for combos
					if (root.TryGetProperty("directions", out var dirsEl))
					{
						foreach (var d in dirsEl.EnumerateArray())
						{
							switch (d.GetString())
							{
								case "left": ControlLeft = true; break;
								case "right": ControlRight = true; break;
								case "jump": ControlJump = true; break;
								case "up": ControlUp = true; break;
								case "down": ControlDown = true; break;
							}
						}
					}
					else
					{
						string dir = root.GetProperty("direction").GetString();
						switch (dir)
						{
							case "left": ControlLeft = true; break;
							case "right": ControlRight = true; break;
							case "jump": ControlJump = true; break;
							case "up": ControlUp = true; break;
							case "down": ControlDown = true; break;
							case "stop": break; // all reset already
						}
					}
					break;
				}
				case "use_item":
				{
					ControlUseItem = true;
					if (root.TryGetProperty("target_x", out var tx))
						TargetTileX = tx.GetInt32();
					if (root.TryGetProperty("target_y", out var ty))
						TargetTileY = ty.GetInt32();
					break;
				}
				case "stop_use":
				{
					ControlUseItem = false;
					TargetTileX = -1;
					TargetTileY = -1;
					break;
				}
				case "select_item":
				{
					int slot = root.GetProperty("slot").GetInt32();
					if (slot >= 0 && slot < 50)
						Main.LocalPlayer.selectedItem = slot;
					break;
				}
				case "quick_heal":
				{
					ControlQuickHeal = true;  // consumed in SetControls, auto-resets
					break;
				}
				case "set_auto_mode":
				{
					AutoMode = root.GetProperty("enabled").GetBoolean();
					SendEvent("auto_mode_changed", new { enabled = AutoMode });
					break;
				}
				case "get_state":
				{
					PushState(force: true);
					break;
				}
				case "get_nearby_tiles":
				{
					int radius = 10;
					if (root.TryGetProperty("radius", out var r))
						radius = r.GetInt32();
					PushNearbyTiles(radius);
					break;
				}
				case "check_tile":
				{
					int tx = root.GetProperty("x").GetInt32();
					int ty = root.GetProperty("y").GetInt32();
					bool valid = tx >= 0 && ty >= 0 && tx < Main.maxTilesX && ty < Main.maxTilesY;
					if (valid)
					{
						var tile = Main.tile[tx, ty];
						Send(new
						{
							type = "tile_info",
							x = tx,
							y = ty,
							hasTile = tile.HasTile,
							tileType = tile.HasTile ? tile.TileType : -1,
							wallType = (int)tile.WallType,
							liquid = tile.LiquidAmount
						});
					}
					break;
				}
				case "find_pickaxe":
				{
					// Find the best pickaxe in inventory and return its slot
					var p = Main.LocalPlayer;
					int bestSlot = -1;
					int bestPick = 0;
					for (int i = 0; i < 50; i++)
					{
						var item = p.inventory[i];
						if (item != null && !item.IsAir && item.pick > bestPick)
						{
							bestPick = item.pick;
							bestSlot = i;
						}
					}
					Send(new
					{
						type = "tool_info",
						tool = "pickaxe",
						slot = bestSlot,
						power = bestPick,
						name = bestSlot >= 0 ? p.inventory[bestSlot].Name : ""
					});
					break;
				}
				case "find_axe":
				{
					var p = Main.LocalPlayer;
					int bestSlot = -1;
					int bestAxe = 0;
					for (int i = 0; i < 50; i++)
					{
						var item = p.inventory[i];
						if (item != null && !item.IsAir && item.axe > bestAxe)
						{
							bestAxe = item.axe;
							bestSlot = i;
						}
					}
					Send(new
					{
						type = "tool_info",
						tool = "axe",
						slot = bestSlot,
						power = bestAxe,
						name = bestSlot >= 0 ? p.inventory[bestSlot].Name : ""
					});
					break;
				}
				// === Direct game API commands (instant, no animation) ===
			case "kill_tile":
				{
					int tx = root.GetProperty("x").GetInt32();
					int ty = root.GetProperty("y").GetInt32();
					bool valid = tx >= 0 && ty >= 0 && tx < Main.maxTilesX && ty < Main.maxTilesY;
					bool success = false;
					if (valid && Main.tile[tx, ty].HasTile)
					{
						WorldGen.KillTile(tx, ty);
						success = !Main.tile[tx, ty].HasTile;
					}
					Send(new { type = "kill_tile_result", x = tx, y = ty, success });
					break;
				}
			case "kill_area":
				{
					int x1 = root.GetProperty("x1").GetInt32();
					int y1 = root.GetProperty("y1").GetInt32();
					int x2 = root.GetProperty("x2").GetInt32();
					int y2 = root.GetProperty("y2").GetInt32();
					int killed = 0;
					for (int x = Math.Min(x1, x2); x <= Math.Max(x1, x2); x++)
						for (int y = Math.Min(y1, y2); y <= Math.Max(y1, y2); y++)
						{
							if (x >= 0 && y >= 0 && x < Main.maxTilesX && y < Main.maxTilesY
								&& Main.tile[x, y].HasTile)
							{
								WorldGen.KillTile(x, y);
								if (!Main.tile[x, y].HasTile) killed++;
							}
						}
					Send(new { type = "kill_area_result", killed });
					break;
				}
			case "place_tile":
				{
					int tx = root.GetProperty("x").GetInt32();
					int ty = root.GetProperty("y").GetInt32();
					int tileType = root.GetProperty("tile_type").GetInt32();
					int style = 0;
					if (root.TryGetProperty("style", out var s))
						style = s.GetInt32();
					bool success = false;
					if (tx >= 0 && ty >= 0 && tx < Main.maxTilesX && ty < Main.maxTilesY)
					{
						success = WorldGen.PlaceTile(tx, ty, tileType, style: style);
					}
					Send(new { type = "place_tile_result", x = tx, y = ty, success });
					break;
				}
			case "place_wall":
				{
					int tx = root.GetProperty("x").GetInt32();
					int ty = root.GetProperty("y").GetInt32();
					int wallType = root.GetProperty("wall_type").GetInt32();
					if (tx >= 0 && ty >= 0 && tx < Main.maxTilesX && ty < Main.maxTilesY)
					{
						WorldGen.PlaceWall(tx, ty, wallType);
					}
					Send(new { type = "place_wall_result", x = tx, y = ty,
						success = Main.tile[tx, ty].WallType > 0 });
					break;
				}
			case "find_trees":
				{
					var p = Main.LocalPlayer;
					int cx = (int)(p.position.X / 16f);
					int cy = (int)(p.position.Y / 16f);
					int radius = 25;
					if (root.TryGetProperty("radius", out var r))
						radius = r.GetInt32();
					var trees = new List<object>();
					var seenX = new HashSet<int>();
					for (int x = cx - radius; x <= cx + radius; x++)
					{
						if (x < 0 || x >= Main.maxTilesX || seenX.Contains(x)) continue;
						int baseY = -1;
						for (int y = cy - 30; y <= cy + 10; y++)
						{
							if (y < 0 || y >= Main.maxTilesY) continue;
							var tile = Main.tile[x, y];
							if (tile.HasTile && tile.TileType == TileID.Trees)
								baseY = y;
						}
						if (baseY >= 0)
						{
							trees.Add(new { x, y = baseY, distance = Math.Abs(x - cx) });
							seenX.Add(x);
						}
					}
					Send(new { type = "tree_positions", trees });
					break;
				}
			case "get_inventory":
				{
					var p = Main.LocalPlayer;
					var items = new List<object>();
					for (int i = 0; i < 50; i++)
					{
						var item = p.inventory[i];
						if (item != null && !item.IsAir)
						{
							items.Add(new
							{
								slot = i, name = item.Name, id = item.type,
								stack = item.stack, maxStack = item.maxStack,
								damage = item.damage, pick = item.pick, axe = item.axe,
								hammer = item.hammer, defense = item.defense,
								healLife = item.healLife, healMana = item.healMana,
								rare = item.rare, accessory = item.accessory,
								createTile = item.createTile, createWall = item.createWall,
								consumable = item.consumable, ammo = item.ammo,
								headSlot = item.headSlot, bodySlot = item.bodySlot,
								legSlot = item.legSlot
							});
						}
					}
					Send(new { type = "inventory", items });
					break;
				}
			case "find_item":
				{
					var p = Main.LocalPlayer;
					string name = "";
					int searchId = -1;
					if (root.TryGetProperty("name", out var n))
						name = n.GetString();
					if (root.TryGetProperty("id", out var sid))
						searchId = sid.GetInt32();
					var found = new List<object>();
					for (int i = 0; i < 50; i++)
					{
						var item = p.inventory[i];
						if (item == null || item.IsAir) continue;
						bool match = false;
						if (searchId > 0 && item.type == searchId) match = true;
						if (!string.IsNullOrEmpty(name) &&
							item.Name.Contains(name, StringComparison.OrdinalIgnoreCase)) match = true;
						if (match)
							found.Add(new { slot = i, name = item.Name, id = item.type,
								stack = item.stack, createTile = item.createTile,
								createWall = item.createWall });
					}
					Send(new { type = "item_search", found });
					break;
				}
			case "get_recipes":
				{
					// Scan ALL recipes in the game, check material availability
					string filter = "";
					if (root.TryGetProperty("filter", out var f))
						filter = f.GetString();
					int limit = 30;
					if (root.TryGetProperty("limit", out var lim))
						limit = lim.GetInt32();
					// "category" filter: weapon/pick/axe/armor/potion/all
					string category = "all";
					if (root.TryGetProperty("category", out var cat))
						category = cat.GetString();

					var p = Main.LocalPlayer;
					var recipes = new List<object>();

					for (int i = 0; i < Recipe.numRecipes && recipes.Count < limit; i++)
					{
						var recipe = Main.recipe[i];
						if (recipe.createItem == null || recipe.createItem.IsAir) continue;
						var ci = recipe.createItem;

						// Category filter
						if (category == "weapon" && (ci.damage <= 0 || ci.pick > 0 || ci.axe > 0 || ci.hammer > 0)) continue;
						if (category == "pick" && ci.pick <= 0) continue;
						if (category == "axe" && ci.axe <= 0) continue;
						if (category == "armor" && ci.defense <= 0) continue;
						if (category == "potion" && ci.healLife <= 0) continue;

						// Name/ID filter
						if (!string.IsNullOrEmpty(filter) &&
							!ci.Name.Contains(filter, StringComparison.OrdinalIgnoreCase) &&
							ci.type.ToString() != filter)
							continue;

						// Check each ingredient: how much we have vs need
						var ingredients = new List<object>();
						bool hasMaterials = true;
						foreach (var req in recipe.requiredItem)
						{
							if (req == null || req.IsAir) continue;
							int have = 0;
							for (int s = 0; s < 50; s++)
							{
								if (p.inventory[s].type == req.type)
									have += p.inventory[s].stack;
							}
							ingredients.Add(new {
								name = req.Name, id = req.type,
								need = req.stack, have = have,
								enough = have >= req.stack
							});
							if (have < req.stack) hasMaterials = false;
						}

						// Check required crafting stations (adjTile)
						var stations = new List<object>();
						bool hasStations = true;
						foreach (int tileId in recipe.requiredTile)
						{
							if (tileId < 0) continue;
							bool nearby = p.adjTile[tileId];
							string tileName = TileID.Search.TryGetName(tileId, out var tn) ? tn : $"Tile_{tileId}";
							stations.Add(new { id = tileId, name = tileName, nearby });
							if (!nearby) hasStations = false;
						}
						// Check liquid requirements via Conditions
						foreach (var cond in recipe.Conditions)
						{
							if (cond == Condition.NearWater && !p.adjWater) hasStations = false;
							if (cond == Condition.NearLava && !p.adjLava) hasStations = false;
							if (cond == Condition.NearHoney && !p.adjHoney) hasStations = false;
						}

						bool canCraft = hasMaterials && hasStations;

						recipes.Add(new
						{
							index = i,
							canCraft, hasMaterials, hasStations,
							result = new {
								name = ci.Name, id = ci.type, count = ci.stack,
								damage = ci.damage, pick = ci.pick, axe = ci.axe,
								hammer = ci.hammer, defense = ci.defense,
								healLife = ci.healLife, mana = ci.mana,
								createTile = ci.createTile, createWall = ci.createWall,
								accessory = ci.accessory, potion = ci.potion,
								ammo = ci.ammo, consumable = ci.consumable
							},
							ingredients, stations
						});
					}

					Send(new { type = "recipes", total = recipes.Count, recipes });
					break;
				}
			case "craft":
				{
					int itemId = -1;
					string itemName = "";
					if (root.TryGetProperty("item_id", out var idEl))
						itemId = idEl.GetInt32();
					if (root.TryGetProperty("item_name", out var nameEl))
						itemName = nameEl.GetString();
					int amount = 1;
					if (root.TryGetProperty("amount", out var amtEl))
						amount = amtEl.GetInt32();

					Recipe.FindRecipes();
					int crafted = 0;
					for (int a = 0; a < amount; a++)
					{
						if (a > 0) Recipe.FindRecipes();
						int recipeIdx = -1;
						for (int i = 0; i < Main.numAvailableRecipes; i++)
						{
							var rc = Main.recipe[Main.availableRecipe[i]];
							if (itemId > 0 && rc.createItem.type == itemId)
							{ recipeIdx = Main.availableRecipe[i]; break; }
							if (!string.IsNullOrEmpty(itemName) &&
								rc.createItem.Name.Contains(itemName, StringComparison.OrdinalIgnoreCase))
							{ recipeIdx = Main.availableRecipe[i]; break; }
						}
						if (recipeIdx < 0) break;

						var recipe = Main.recipe[recipeIdx];
						var p = Main.LocalPlayer;
						// Consume ingredients
						foreach (var req in recipe.requiredItem)
						{
							if (req == null || req.IsAir) continue;
							int remaining = req.stack;
							for (int i = 0; i < 50 && remaining > 0; i++)
							{
								if (p.inventory[i].type == req.type)
								{
									int take = Math.Min(remaining, p.inventory[i].stack);
									p.inventory[i].stack -= take;
									remaining -= take;
									if (p.inventory[i].stack <= 0)
										p.inventory[i].TurnToAir();
								}
							}
						}
						// Add result to inventory
						var result = new Item();
						result.SetDefaults(recipe.createItem.type);
						result.stack = recipe.createItem.stack;
						bool placed = false;
						for (int i = 0; i < 50 && !placed; i++)
						{
							if (p.inventory[i].type == result.type &&
								p.inventory[i].stack < p.inventory[i].maxStack)
							{
								p.inventory[i].stack += Math.Min(
									p.inventory[i].maxStack - p.inventory[i].stack, result.stack);
								placed = true;
							}
						}
						if (!placed)
							for (int i = 0; i < 50; i++)
								if (p.inventory[i].IsAir)
								{ p.inventory[i] = result.Clone(); placed = true; break; }
						if (placed) crafted++;
						else break;
					}
					Send(new { type = "craft_result", success = crafted > 0,
						item = itemName != "" ? itemName : itemId.ToString(), crafted });
					break;
				}
			case "swap_slots":
				{
					int from = root.GetProperty("from").GetInt32();
					int to = root.GetProperty("to").GetInt32();
					var p = Main.LocalPlayer;
					if (from >= 0 && from < 50 && to >= 0 && to < 50)
					{
						var temp = p.inventory[to];
						p.inventory[to] = p.inventory[from];
						p.inventory[from] = temp;
						Send(new { type = "swap_result", success = true, from, to });
					}
					else
					{
						Send(new { type = "swap_result", success = false });
					}
					break;
				}
			case "get_equip":
				{
					// Returns current armor/accessory slots
					// armor[0]=Head, [1]=Body, [2]=Legs, [3..9]=Accessory, [10..19]=Vanity
					var p = Main.LocalPlayer;
					var slots = new List<object>();
					for (int i = 0; i < 10; i++)
					{
						var item = p.armor[i];
						if (item != null && !item.IsAir)
						{
							string slotName = i switch
							{
								0 => "head", 1 => "body", 2 => "legs",
								_ => $"accessory{i - 3}"
							};
							slots.Add(new
							{
								slot = i, slotName,
								name = item.Name, id = item.type,
								defense = item.defense,
								accessory = item.accessory,
								rare = item.rare
							});
						}
					}
					Send(new { type = "equip_info", slots });
					break;
				}
			case "equip_item":
				{
					// Equip item from inventory to armor/accessory slot
					// from: inventory slot (0-49), to: armor slot index (0=head,1=body,2=legs,3-9=accessory)
					int from = root.GetProperty("from").GetInt32();
					int to = root.GetProperty("to").GetInt32();
					var p = Main.LocalPlayer;
					if (from >= 0 && from < 50 && to >= 0 && to < 10)
					{
						var temp = p.armor[to];
						p.armor[to] = p.inventory[from];
						p.inventory[from] = temp;
						Send(new { type = "equip_result", success = true, from, to,
							equipped = p.armor[to].Name });
					}
					else
					{
						Send(new { type = "equip_result", success = false });
					}
					break;
				}
			case "teleport":
				{
					float tx = (float)root.GetProperty("x").GetDouble();
					float ty = (float)root.GetProperty("y").GetDouble();
					Main.LocalPlayer.Teleport(
						new Microsoft.Xna.Framework.Vector2(tx * 16f, ty * 16f));
					Send(new { type = "teleport_result", success = true });
					break;
				}
			case "kill_hostile_npcs":
				{
					var p = Main.LocalPlayer;
					float range = 800f;
					if (root.TryGetProperty("range", out var rng))
						range = (float)rng.GetDouble();
					int killed = 0;
					var killedList = new List<object>();
					for (int i = 0; i < Main.maxNPCs; i++)
					{
						var npc = Main.npc[i];
						if (npc.active && !npc.friendly && !npc.townNPC
							&& npc.Distance(p.Center) < range)
						{
							killedList.Add(new { name = npc.FullName, id = npc.type,
								life = npc.life });
							npc.StrikeInstantKill();
							killed++;
						}
					}
					Send(new { type = "kill_npcs_result", killed, npcs = killedList });
					break;
				}
			case "collect_items":
				{
					// Teleport nearby dropped items directly into player inventory
					var p = Main.LocalPlayer;
					float range = 400f;
					if (root.TryGetProperty("range", out var rng2))
						range = (float)rng2.GetDouble();
					int collected = 0;
					for (int i = 0; i < Main.maxItems; i++)
					{
						var item = Main.item[i];
						if (item.active && !item.IsAir
							&& item.Distance(p.Center) < range)
						{
							// Move item to player position so it gets auto-picked
							item.position = p.position;
							collected++;
						}
					}
					Send(new { type = "collect_result", collected });
					break;
				}
			case "scan_area":
				{
					int sx = root.GetProperty("x").GetInt32();
					int sy = root.GetProperty("y").GetInt32();
					int sw = root.GetProperty("width").GetInt32();
					int sh = root.GetProperty("height").GetInt32();
					int x1 = Math.Max(0, sx);
					int y1 = Math.Max(0, sy);
					int x2 = Math.Min(Main.maxTilesX - 1, sx + sw - 1);
					int y2 = Math.Min(Main.maxTilesY - 1, sy + sh - 1);
					var scanTiles = new List<object>();
					for (int scanY = y1; scanY <= y2; scanY++)
						for (int scanX = x1; scanX <= x2; scanX++)
						{
							var tile = Main.tile[scanX, scanY];
							if (tile.HasTile)
							{
								int ttype = (int)tile.TileType;
								// c: 0=Block, 1=OneWay(platform), 2=Ore
								int c = 0;
								if (TileID.Sets.Platforms[ttype])
									c = 1;
								else if (TileID.Sets.Ore[ttype])
									c = 2;
								scanTiles.Add(new { x = scanX, y = scanY, t = ttype, c });
							}
						}
					Send(new { type = "scan_result", x = x1, y = y1,
						width = x2 - x1 + 1, height = y2 - y1 + 1, tiles = scanTiles });
					break;
				}
			case "scan_relative":
				{
					// 以玩家中心为原点的相对坐标扫描
					var srPlayer = Main.LocalPlayer;
					int pcx = (int)(srPlayer.Center.X / 16f);
					int pcy = (int)(srPlayer.Center.Y / 16f);
					int srx = root.GetProperty("rx").GetInt32();
					int sry = root.GetProperty("ry").GetInt32();
					int srw = root.GetProperty("w").GetInt32();
					int srh = root.GetProperty("h").GetInt32();
					int sr_x1 = Math.Max(0, pcx + srx);
					int sr_y1 = Math.Max(0, pcy + sry);
					int sr_x2 = Math.Min(Main.maxTilesX - 1, pcx + srx + srw - 1);
					int sr_y2 = Math.Min(Main.maxTilesY - 1, pcy + sry + srh - 1);
					var srTiles = new List<object>();
					for (int scanY = sr_y1; scanY <= sr_y2; scanY++)
						for (int scanX = sr_x1; scanX <= sr_x2; scanX++)
						{
							var tile = Main.tile[scanX, scanY];
							if (tile.HasTile)
							{
								int ttype = (int)tile.TileType;
								int c = 0;
								if (TileID.Sets.Platforms[ttype])
									c = 1;
								else if (TileID.Sets.Ore[ttype])
									c = 2;
								srTiles.Add(new { x = scanX, y = scanY, t = ttype, c });
							}
						}
					Send(new { type = "scan_result", x = sr_x1, y = sr_y1,
						width = sr_x2 - sr_x1 + 1, height = sr_y2 - sr_y1 + 1,
						playerX = pcx, playerY = pcy, tiles = srTiles });
					break;
				}
			case "get_nearest_npcs":
				{
					// C# 端距离排序 + tile 坐标转换
					var gnPlayer = Main.LocalPlayer;
					bool hostileOnly = root.TryGetProperty("hostile", out var hProp) && hProp.GetBoolean();
					int maxCount = root.TryGetProperty("count", out var cProp) ? cProp.GetInt32() : 5;
					float maxRange = root.TryGetProperty("range", out var rProp) ? rProp.GetSingle() * 16f : 800f;
					var npcList = new List<(float dist, NPC npc)>();
					for (int i = 0; i < Main.maxNPCs; i++)
					{
						var npc = Main.npc[i];
						if (!npc.active || npc.life <= 0) continue;
						if (hostileOnly && (npc.friendly || npc.townNPC || npc.damage <= 0)) continue;
						float dist = npc.Distance(gnPlayer.Center);
						if (dist <= maxRange)
							npcList.Add((dist, npc));
					}
					npcList.Sort((a, b) => a.dist.CompareTo(b.dist));
					var resultNpcs = new List<object>();
					int take = Math.Min(maxCount, npcList.Count);
					for (int i = 0; i < take; i++)
					{
						var (dist, npc) = npcList[i];
						resultNpcs.Add(new
						{
							name = npc.FullName,
							id = npc.type,
							life = npc.life,
							lifeMax = npc.lifeMax,
							tileX = (int)(npc.Center.X / 16f),
							tileY = (int)(npc.Center.Y / 16f),
							dist = MathF.Round(dist / 16f, 1),
							friendly = npc.friendly,
							townNPC = npc.townNPC,
							damage = npc.damage,
							boss = npc.boss
						});
					}
					Send(new { type = "nearest_npcs", npcs = resultNpcs });
					break;
				}
			case "give_item":
				{
					int giveId = root.GetProperty("item_id").GetInt32();
					int giveStack = 1;
					if (root.TryGetProperty("stack", out var gsEl))
						giveStack = gsEl.GetInt32();
					var gp = Main.LocalPlayer;
					var gItem = new Item();
					gItem.SetDefaults(giveId);
					gItem.stack = giveStack;
					// Try to add to existing stack first
					bool gPlaced = false;
					for (int gi = 0; gi < 50 && !gPlaced; gi++)
					{
						if (gp.inventory[gi].type == gItem.type &&
							gp.inventory[gi].stack < gp.inventory[gi].maxStack)
						{
							gp.inventory[gi].stack += giveStack;
							gPlaced = true;
						}
					}
					if (!gPlaced)
					{
						for (int gi = 0; gi < 50; gi++)
						{
							if (gp.inventory[gi].IsAir)
							{ gp.inventory[gi] = gItem.Clone(); gPlaced = true; break; }
						}
					}
					Send(new { type = "give_item_result", success = gPlaced, item_id = giveId, name = gItem.Name });
					break;
				}
			case "trash_item":
				{
					int trashSlot = root.GetProperty("slot").GetInt32();
					var p = Main.LocalPlayer;
					if (trashSlot >= 0 && trashSlot < 50 && p.inventory[trashSlot] != null && !p.inventory[trashSlot].IsAir)
					{
						string trashed = p.inventory[trashSlot].Name;
						int trashedStack = p.inventory[trashSlot].stack;
						p.inventory[trashSlot].TurnToAir();
						Send(new { type = "trash_result", success = true, slot = trashSlot,
							name = trashed, stack = trashedStack });
					}
					else
						Send(new { type = "trash_result", success = false, slot = trashSlot });
					break;
				}
			case "open_chest":
				{
					// Open chest UI (visual) — player.chest = chestIdx triggers the game's chest UI
					int ocx = root.GetProperty("x").GetInt32();
					int ocy = root.GetProperty("y").GetInt32();
					int ocIdx = Chest.FindChest(ocx, ocy);
					int ocTriedOffset = 0;
					if (ocIdx < 0) { ocIdx = Chest.FindChest(ocx - 1, ocy); ocTriedOffset = 1; }
					if (ocIdx < 0) { ocIdx = Chest.FindChest(ocx, ocy - 1); ocTriedOffset = 2; }
					if (ocIdx < 0) { ocIdx = Chest.FindChest(ocx - 1, ocy - 1); ocTriedOffset = 3; }
					Mod.Logger.Info($"[open_chest·诊断] req=({ocx},{ocy}) FindChest→{ocIdx} offsetUsed={ocTriedOffset} netMode={Main.netMode}");
					if (ocIdx >= 0)
					{
						var ocPlayer = Main.LocalPlayer;
						ocPlayer.chest = ocIdx;
						Main.playerInventory = true;
						Main.recBigList = false;
						var ocChest = Main.chest[ocIdx];
						int ocFilled = 0, ocEmpty = 0;
						for (int oi = 0; oi < Chest.maxItems; oi++)
						{
							if (ocChest.item[oi] == null || ocChest.item[oi].IsAir) ocEmpty++;
							else ocFilled++;
						}
						Mod.Logger.Info($"[open_chest·诊断] chest@({ocChest.x},{ocChest.y}) filled={ocFilled} empty={ocEmpty}");
						Send(new { type = "open_chest_result", success = true, chestIndex = ocIdx });
					}
					else
					{
						Send(new { type = "open_chest_result", success = false, reason = "no_chest" });
					}
					break;
				}
			case "close_chest":
				{
					var ccPlayer = Main.LocalPlayer;
					ccPlayer.chest = -1;
					Main.playerInventory = false;
					Send(new { type = "close_chest_result", success = true });
					break;
				}
			case "loot_chest":
				{
					int lcx = root.GetProperty("x").GetInt32();
					int lcy = root.GetProperty("y").GetInt32();
					int chestIdx = Chest.FindChest(lcx, lcy);
					if (chestIdx < 0) chestIdx = Chest.FindChest(lcx - 1, lcy);
					if (chestIdx < 0) chestIdx = Chest.FindChest(lcx, lcy - 1);
					if (chestIdx < 0) chestIdx = Chest.FindChest(lcx - 1, lcy - 1);
					if (chestIdx < 0)
					{
						Send(new { type = "loot_result", success = false, reason = "no_chest" });
						break;
					}
					var chest = Main.chest[chestIdx];
					var p = Main.LocalPlayer;
					var looted = new List<object>();
					for (int ci = 0; ci < Chest.maxItems; ci++)
					{
						var cItem = chest.item[ci];
						if (cItem == null || cItem.IsAir) continue;
						string cName = cItem.Name;
						int cId = cItem.type;
						int cStack = cItem.stack;
						bool placed = false;
						for (int s = 0; s < 50 && !placed; s++)
						{
							if (p.inventory[s].type == cItem.type &&
								p.inventory[s].stack < p.inventory[s].maxStack)
							{
								int take = Math.Min(p.inventory[s].maxStack - p.inventory[s].stack, cItem.stack);
								p.inventory[s].stack += take;
								cItem.stack -= take;
								if (cItem.stack <= 0) { cItem.TurnToAir(); placed = true; }
							}
						}
						if (!placed && !cItem.IsAir)
						{
							for (int s = 0; s < 50; s++)
								if (p.inventory[s].IsAir)
								{
									p.inventory[s] = cItem.Clone();
									cItem.TurnToAir();
									placed = true;
									break;
								}
						}
						if (placed)
							looted.Add(new { name = cName, id = cId, stack = cStack });
					}
					Send(new { type = "loot_result", success = true,
						chestX = chest.x, chestY = chest.y, items = looted });
					break;
				}
			case "deposit_to_chest":
				{
					int dcx = root.GetProperty("x").GetInt32();
					int dcy = root.GetProperty("y").GetInt32();
					int dChestIdx = Chest.FindChest(dcx, dcy);
					if (dChestIdx < 0) dChestIdx = Chest.FindChest(dcx - 1, dcy);
					if (dChestIdx < 0) dChestIdx = Chest.FindChest(dcx, dcy - 1);
					if (dChestIdx < 0) dChestIdx = Chest.FindChest(dcx - 1, dcy - 1);
					Mod.Logger.Info($"[deposit·诊断] req=({dcx},{dcy}) FindChest→{dChestIdx} netMode={Main.netMode} playerChest={Main.LocalPlayer.chest}");
					if (dChestIdx < 0)
					{
						Send(new { type = "deposit_result", success = false, reason = "no_chest" });
						break;
					}
					var dChest = Main.chest[dChestIdx];
					var dp = Main.LocalPlayer;
					int dDbgFilled = 0, dDbgEmpty = 0;
					for (int dj = 0; dj < Chest.maxItems; dj++)
					{
						if (dChest.item[dj] == null || dChest.item[dj].IsAir) dDbgEmpty++;
						else dDbgFilled++;
					}
					int dInvNonKeep = 0;
					for (int dk = 10; dk < 50; dk++)
					{
						var it = dp.inventory[dk];
						if (it != null && !it.IsAir) dInvNonKeep++;
					}
					Mod.Logger.Info($"[deposit·诊断] chest filled={dDbgFilled} empty={dDbgEmpty} | backpack非空槽(10-49)={dInvNonKeep}");
					var keepSlots = new HashSet<int>();
					if (root.TryGetProperty("keep_slots", out var ksEl))
						foreach (var ksItem in ksEl.EnumerateArray())
							keepSlots.Add(ksItem.GetInt32());
					var deposited = new List<object>();
					for (int di = 0; di < 50; di++)
					{
						if (keepSlots.Contains(di)) continue;
						var pItem = dp.inventory[di];
						if (pItem == null || pItem.IsAir) continue;
						string dName = pItem.Name;
						int dId = pItem.type;
						int dStack = pItem.stack;
						bool placed = false;
						for (int c = 0; c < Chest.maxItems && !placed; c++)
						{
							if (dChest.item[c].type == pItem.type &&
								dChest.item[c].stack < dChest.item[c].maxStack)
							{
								int take = Math.Min(dChest.item[c].maxStack - dChest.item[c].stack, pItem.stack);
								dChest.item[c].stack += take;
								pItem.stack -= take;
								if (pItem.stack <= 0) { pItem.TurnToAir(); placed = true; }
							}
						}
						if (!placed && !pItem.IsAir)
						{
							for (int c = 0; c < Chest.maxItems; c++)
								if (dChest.item[c] == null || dChest.item[c].IsAir)
								{
									dChest.item[c] = pItem.Clone();
									pItem.TurnToAir();
									placed = true;
									break;
								}
						}
						if (placed)
							deposited.Add(new { name = dName, id = dId, stack = dStack, fromSlot = di });
					}
					Send(new { type = "deposit_result", success = true,
						chestX = dChest.x, chestY = dChest.y, deposited });
					break;
				}
			case "navigate_to":
				{
					try
					{
						var navPlayer = Main.LocalPlayer;
						int navTargetX = root.GetProperty("x").GetInt32();
						int navTargetY = root.GetProperty("y").GetInt32();
						bool navAllowDig = root.TryGetProperty("allow_dig", out var navAdProp) && navAdProp.GetBoolean();
						int navRange = root.TryGetProperty("range", out var navRProp) ? navRProp.GetInt32() : 100;
						int navAirPenalty = root.TryGetProperty("air_penalty", out var navApProp) ? navApProp.GetInt32() : 1;

						int navStartX = (int)(navPlayer.Center.X / 16f);
						int navStartY = (int)(navPlayer.position.Y / 16f) + 2;

						int navPickPower = 0;
						if (navAllowDig)
						{
							for (int i = 0; i < 50; i++)
							{
								var item = navPlayer.inventory[i];
								if (item != null && !item.IsAir && item.pick > navPickPower)
									navPickPower = item.pick;
							}
						}

						bool navHasWings = Pathfinder.PlayerHasWings();
						Mod.Logger.Info($"[navigate_to] hasWings={navHasWings} wingTimeMax={navPlayer.wingTimeMax}");

						var navResult = Pathfinder.FindPath(
							navStartX, navStartY, navTargetX, navTargetY,
							navRange, navAllowDig, navPickPower, navAirPenalty, navHasWings);

						if (navResult.Success)
						{
							_navigator.Start(navResult.Waypoints, navTargetX, navTargetY, navAllowDig, navPickPower, navAirPenalty, navHasWings);
							var wpList = navResult.Waypoints.Select(p => new { x = p.X, y = p.Y }).ToArray();
							Send(new { type = "nav_status", status = "started",
								waypoints = navResult.Waypoints.Count,
								waypointList = wpList,
								targetX = navTargetX, targetY = navTargetY,
								hasWings = navHasWings,
								maxJumpHeight = Pathfinder.CalculateMaxJumpHeight() });
						}
						else
						{
							Send(new { type = "nav_status", status = "stuck",
								reason = navResult.Reason ?? "no_path",
								startX = navStartX, startY = navStartY,
								targetX = navTargetX, targetY = navTargetY,
								allowDig = navAllowDig, pickPower = navPickPower,
								range = navRange,
								debug = navResult.DebugInfo });
						}
					}
					catch (Exception navEx)
					{
						Mod.Logger.Error($"navigate_to error: {navEx}");
						Send(new { type = "nav_status", status = "stuck",
							reason = $"exception: {navEx.Message}" });
					}
					break;
				}
			case "cancel_navigate":
				{
					if (_navigator.Active)
					{
						_navigator.Cancel();
						ResetMovement();
						ControlUseItem = false;
						TargetTileX = -1;
						TargetTileY = -1;
						Send(new { type = "nav_status", status = "cancelled" });
					}
					break;
				}
			case "equip_wings":
				{
					// Equip wings by item ID (default: Leaf Wings)
					try
					{
						var ewPlayer = Main.LocalPlayer;
						int wingItemId = root.TryGetProperty("item_id", out var widProp)
							? widProp.GetInt32()
							: ItemID.LeafWings;
						// Find first empty accessory slot (armor[3..9])
						int slot = -1;
						for (int i = 3; i < 10; i++)
						{
							if (ewPlayer.armor[i] == null || ewPlayer.armor[i].IsAir)
							{
								slot = i;
								break;
							}
						}
						// Check if wings already equipped
						for (int i = 3; i < 10; i++)
						{
							var existing = ewPlayer.armor[i];
							if (existing != null && !existing.IsAir && existing.wingSlot > 0)
							{
								Send(new { type = "equip_result", success = true,
									item = existing.Name, itemId = existing.type, slot = i,
									wingSlot = existing.wingSlot,
									wingTimeMax = ewPlayer.wingTimeMax,
									hasWings = true, alreadyEquipped = true });
								goto doneEquipWings;
							}
						}
						if (slot == -1) slot = 3; // overwrite first accessory if all full
						var wingItem = new Item();
						wingItem.SetDefaults(wingItemId);
						ewPlayer.armor[slot] = wingItem;
						Mod.Logger.Info($"[equip_wings] Equipped {wingItem.Name} (id={wingItemId}, LeafWings={ItemID.LeafWings}, wingSlot={wingItem.wingSlot}) in slot {slot}");
						Send(new { type = "equip_result", success = true,
							item = wingItem.Name, itemId = wingItemId, slot,
							wingSlot = wingItem.wingSlot,
							wingTimeMax = ewPlayer.wingTimeMax,
							hasWings = Pathfinder.PlayerHasWings() });
						doneEquipWings:;
					}
					catch (Exception ewEx)
					{
						Send(new { type = "equip_result", success = false, error = ewEx.Message });
					}
					break;
				}
			case "find_path":
				{
					// A* 寻路 — 只计算路径，不执行
					try
					{
						var fpPlayer = Main.LocalPlayer;
						int fpTargetX = root.GetProperty("x").GetInt32();
						int fpTargetY = root.GetProperty("y").GetInt32();
						bool fpAllowDig = root.TryGetProperty("allow_dig", out var adProp) && adProp.GetBoolean();
						int fpRange = root.TryGetProperty("range", out var fpRProp) ? fpRProp.GetInt32() : 100;

						int fpStartX = (int)(fpPlayer.Center.X / 16f);
						int fpStartY = (int)(fpPlayer.position.Y / 16f) + 2; // feet position

						// Get pickaxe power for dig cost
						int pickPower = 0;
						if (fpAllowDig)
						{
							for (int i = 0; i < 50; i++)
							{
								var item = fpPlayer.inventory[i];
								if (item != null && !item.IsAir && item.pick > pickPower)
									pickPower = item.pick;
							}
						}

						bool fpHasWings = Pathfinder.PlayerHasWings();
						var pathResult = Pathfinder.FindPath(
							fpStartX, fpStartY, fpTargetX, fpTargetY,
							fpRange, fpAllowDig, pickPower, 1, fpHasWings);

						if (pathResult.Success)
						{
							var waypointList = new List<object>();
							foreach (var wp in pathResult.Waypoints)
								waypointList.Add(new { x = wp.X, y = wp.Y });
							Send(new { type = "path_result", success = true,
								waypoints = waypointList,
								rawLength = pathResult.RawLength,
								startX = fpStartX, startY = fpStartY });
						}
						else
						{
							Send(new { type = "path_result", success = false,
								reason = pathResult.Reason ?? "unknown",
								startX = fpStartX, startY = fpStartY });
						}
					}
					catch (Exception fpEx)
					{
						Mod.Logger.Error($"find_path error: {fpEx}");
						Send(new { type = "path_result", success = false,
							reason = $"exception: {fpEx.Message}" });
					}
					break;
				}
			case "ping":
				{
					SendEvent("pong", new { tick = Main.GameUpdateCount, version = MOD_VERSION });
					break;
				}
			case "get_spawn":
				{
					Send(new { type = "spawn_result", x = Main.spawnTileX, y = Main.spawnTileY });
					break;
				}
			case "scan_chests":
				{
					int scx = root.GetProperty("cx").GetInt32();
					int scy = root.GetProperty("cy").GetInt32();
					int rangeX = 50;
					int rangeY = 30;
					if (root.TryGetProperty("range_x", out var rxEl)) rangeX = rxEl.GetInt32();
					if (root.TryGetProperty("range_y", out var ryEl)) rangeY = ryEl.GetInt32();

					var chestList = new List<object>();
					for (int i = 0; i < Main.maxChests; i++)
					{
						var ch = Main.chest[i];
						if (ch == null) continue;
						if (Math.Abs(ch.x - scx) > rangeX || Math.Abs(ch.y - scy) > rangeY) continue;

						int itemCount = 0;
						int emptySlots = 0;
						for (int s = 0; s < Chest.maxItems; s++)
						{
							if (ch.item[s] == null || ch.item[s].IsAir)
								emptySlots++;
							else
								itemCount++;
						}
						chestList.Add(new { x = ch.x, y = ch.y, items_count = itemCount,
							empty_slots = emptySlots, has_space = emptySlots > 0 });
					}
					Send(new { type = "scan_chests_result", success = true, chests = chestList });
					break;
				}
			case "quick_stack":
				{
					var qp = Main.LocalPlayer;
					if (qp.chest < 0)
					{
						Send(new { type = "quick_stack_result", success = false, reason = "no_chest_open" });
						break;
					}
					var qChest = Main.chest[qp.chest];
					int stackedCount = 0;

					for (int pi = 10; pi < 50; pi++) // skip hotbar
					{
						var pItem = qp.inventory[pi];
						if (pItem == null || pItem.IsAir) continue;

						// Check if this item type exists in chest
						bool existsInChest = false;
						for (int ci = 0; ci < Chest.maxItems; ci++)
						{
							if (qChest.item[ci] != null && !qChest.item[ci].IsAir &&
								qChest.item[ci].type == pItem.type)
							{ existsInChest = true; break; }
						}
						if (!existsInChest) continue;

						// Stack into chest
						for (int ci = 0; ci < Chest.maxItems; ci++)
						{
							if (pItem.IsAir) break;
							if (qChest.item[ci] != null && qChest.item[ci].type == pItem.type &&
								qChest.item[ci].stack < qChest.item[ci].maxStack)
							{
								int take = Math.Min(qChest.item[ci].maxStack - qChest.item[ci].stack, pItem.stack);
								qChest.item[ci].stack += take;
								pItem.stack -= take;
								if (pItem.stack <= 0) pItem.TurnToAir();
								stackedCount++;
							}
						}
					}
					// Refresh recipe list after inventory change
					Recipe.FindRecipes();
					Send(new { type = "quick_stack_result", success = true, stacked_count = stackedCount });
					break;
				}
			case "place_chest":
				{
					int pcx = root.GetProperty("x").GetInt32();
					int pcy = root.GetProperty("y").GetInt32();
					var pp = Main.LocalPlayer;

					// Validate: 2x2 area must be air
					for (int dx = 0; dx < 2; dx++)
						for (int dy = 0; dy < 2; dy++)
						{
							var t = Main.tile[pcx + dx, pcy + dy];
							if (t != null && t.HasTile)
							{
								Send(new { type = "place_chest_result", success = false,
									reason = $"tile_blocked_at_{pcx+dx}_{pcy+dy}" });
								goto end_place_chest;
							}
						}
					// Validate: 2 solid tiles below
					for (int dx = 0; dx < 2; dx++)
					{
						var bt = Main.tile[pcx + dx, pcy + 2];
						if (bt == null || !bt.HasTile || !Main.tileSolid[bt.TileType])
						{
							Send(new { type = "place_chest_result", success = false,
								reason = $"no_solid_ground_at_{pcx+dx}_{pcy+2}" });
							goto end_place_chest;
						}
					}
					// Find chest item in inventory (ID=48 = wooden chest)
					int chestSlot = -1;
					for (int i = 0; i < 50; i++)
					{
						// Accept any chest item (createTile == 21), not just wooden chest
						if (pp.inventory[i].createTile == TileID.Containers && pp.inventory[i].stack > 0)
						{ chestSlot = i; break; }
					}
					if (chestSlot < 0)
					{
						Send(new { type = "place_chest_result", success = false, reason = "no_chest_item" });
						goto end_place_chest;
					}
					// Place chest using WorldGen (defaults: type=21 wooden chest, style=0)
					int newChestIdx = WorldGen.PlaceChest(pcx, pcy + 1);
					if (newChestIdx >= 0)
					{
						pp.inventory[chestSlot].stack--;
						if (pp.inventory[chestSlot].stack <= 0)
							pp.inventory[chestSlot].TurnToAir();
						Send(new { type = "place_chest_result", success = true,
							chest_x = Main.chest[newChestIdx].x, chest_y = Main.chest[newChestIdx].y });
					}
					else
					{
						Send(new { type = "place_chest_result", success = false, reason = "place_failed" });
					}
					end_place_chest:
					break;
				}
			case "hold_use_item":
				{
					int frames = 180; // default ~3 seconds
					if (root.TryGetProperty("frames", out var frEl)) frames = frEl.GetInt32();
					HoldUseFrames = frames;
					Send(new { type = "hold_use_item_result", success = true, frames });
					break;
				}
				default:
					Mod.Logger.Warn($"LumiBridge unknown command: {cmd}");
					break;
			}
		}

		// ══════════════════════════════════════════════════════════════
		// A* Pathfinder for 2D platformer (JumpLength encoding)
		// ══════════════════════════════════════════════════════════════

		private static class Pathfinder
		{
			// Tile classification
			private const byte EMPTY = 0;
			private const byte BLOCK = 1;
			private const byte ONE_WAY = 2;

			// A* node status
			private const byte UNTOUCHED = 0;
			private const byte OPEN = 1;
			private const byte CLOSED = 2;

			private const int MAX_SEARCH_STEPS = 50000;

			/// <summary>
			/// Calculate max jump height in tiles from current player stats.
			/// Simulates discrete frame physics: powered phase (jumpHeight frames at jumpSpeed)
			/// then coasting phase (velocity decays by gravity until 0).
			/// </summary>
			internal static int CalculateMaxJumpHeight()
			{
				var player = Main.LocalPlayer;
				if (player == null || !player.active)
					return 6; // fallback

				float jumpSpeed = Player.jumpSpeed;     // default 5.01, modified by accessories
				int jumpHeight = Player.jumpHeight;      // default 15 frames, modified by accessories
				float gravity = Player.defaultGravity;   // 0.4

				if (gravity <= 0f) return 6;

				// Simulate jump: powered phase + coasting phase
				float totalPixels = 0f;

				// Powered phase: constant upward velocity for jumpHeight frames
				totalPixels += jumpSpeed * jumpHeight;

				// Coasting phase: velocity decays from jumpSpeed by gravity each frame
				float vel = jumpSpeed;
				while (vel > 0f)
				{
					vel -= gravity;
					if (vel > 0f)
						totalPixels += vel;
				}

				// Convert pixels to tiles (16 px/tile), floor for safety margin
				int tiles = (int)(totalPixels / 16f);
				return Math.Max(1, tiles);
			}

			public struct PathResult
			{
				public bool Success;
				public string Reason;
				public List<Point> Waypoints;
				public int RawLength;
				public string DebugInfo;
			}

			private struct NodeKey : IEquatable<NodeKey>, IComparable<NodeKey>
			{
				public short X, Y, JumpLen;

				public NodeKey(int x, int y, int jumpLen)
				{
					X = (short)x; Y = (short)y; JumpLen = (short)jumpLen;
				}

				public bool Equals(NodeKey other) => X == other.X && Y == other.Y && JumpLen == other.JumpLen;
				public override int GetHashCode() => ((int)X << 20) | (((int)Y & 0xFFF) << 8) | ((int)JumpLen & 0xFF);
				public override bool Equals(object obj) => obj is NodeKey k && Equals(k);

				public int CompareTo(NodeKey other)
				{
					int c = X.CompareTo(other.X);
					if (c != 0) return c;
					c = Y.CompareTo(other.Y);
					if (c != 0) return c;
					return JumpLen.CompareTo(other.JumpLen);
				}
			}

			private class Node
			{
				public int X, Y;
				public short JumpLength;
				public int G, F;
				public Node Parent;
				public byte Status;
			}

			/// <summary>
			/// Build tile classification grid centered around midpoint between start and target.
			/// </summary>
			private static byte[,] BuildGrid(int originX, int originY, int width, int height,
				out byte[,] tileType, bool allowDig, int pickPower)
			{
				// grid[localX, localY] = movement weight (0 = impassable)
				var grid = new byte[width, height];
				tileType = new byte[width, height]; // EMPTY/BLOCK/ONE_WAY

				for (int lx = 0; lx < width; lx++)
				{
					int wx = originX + lx;
					for (int ly = 0; ly < height; ly++)
					{
						int wy = originY + ly;
						if (wx < 0 || wy < 0 || wx >= Main.maxTilesX || wy >= Main.maxTilesY)
						{
							grid[lx, ly] = 0;
							tileType[lx, ly] = BLOCK;
							continue;
						}

						var tile = Main.tile[wx, wy];
						if (!tile.HasTile)
						{
							// No tile = air
							grid[lx, ly] = 1;
							tileType[lx, ly] = EMPTY;
						}
						else
						{
							int tt = (int)tile.TileType;
							bool isSolid = Main.tileSolid[tt];
							bool isSolidTop = Main.tileSolidTop[tt];

							// Doors: player can open and walk through
							// ClosedDoor=10, OpenDoor=11, TallGateClosed=388, TallGateOpen=389
							if (tt == 10 || tt == 11 || tt == 388 || tt == 389)
							{
								grid[lx, ly] = 2; // passable with slight cost
								tileType[lx, ly] = EMPTY;
							}
							else if (!isSolid)
							{
								// Non-solid tiles (trees, torches, furniture, vines, etc.)
								// Player can walk through these
								grid[lx, ly] = 1;
								tileType[lx, ly] = EMPTY;
							}
							else if (isSolidTop)
							{
								// Solid + SolidTop = OneWay platform
								grid[lx, ly] = 1;
								tileType[lx, ly] = ONE_WAY;
							}
							else if (allowDig && pickPower > 0)
							{
								// Solid block, but we can dig through it
								int hitPoints = GetTileHitPoints(tt);
								int digCost = Math.Max(1, hitPoints * 10 / Math.Max(1, pickPower));
								grid[lx, ly] = (byte)Math.Min(254, 1 + digCost);
								tileType[lx, ly] = BLOCK;
							}
							else
							{
								// Solid block, impassable
								grid[lx, ly] = 0;
								tileType[lx, ly] = BLOCK;
							}
						}
					}
				}
				return grid;
			}

			/// <summary>
			/// Get approximate hit points for a tile type (used for dig cost).
			/// </summary>
			private static int GetTileHitPoints(int type)
			{
				// Common tiles — rough estimates of durability
				// Dirt=0, Stone=1, Iron=6, Gold=7, Meteorite=37, Obsidian=56, Hellstone=58
				// Trees=5, Dungeon bricks=41-44 (very hard)
				if (type == 0) return 1;       // Dirt
				if (type == 2 || type == 23 || type == 109 || type == 199) return 2; // Grass variants
				if (type == 1 || type == 25 || type == 203) return 3; // Stone variants
				if (type == 38 || type == 39 || type == 40) return 4; // Gray/Red/Blue bricks
				if (type == 6 || type == 7 || type == 8 || type == 9 ||
					type == 166 || type == 167 || type == 168 || type == 169) return 3; // Ores
				if (type == 56 || type == 58) return 8; // Obsidian, Hellstone
				if (type == 41 || type == 43 || type == 44) return 20; // Dungeon bricks
				return 3; // default
			}

			private static bool IsGround(byte[,] tileType, int lx, int ly, int w, int h)
			{
				if (lx < 0 || ly < 0 || lx >= w || ly >= h) return true; // out of bounds = solid
				return tileType[lx, ly] == BLOCK || tileType[lx, ly] == ONE_WAY;
			}

			private static bool IsBlock(byte[,] tileType, int lx, int ly, int w, int h)
			{
				if (lx < 0 || ly < 0 || lx >= w || ly >= h) return true;
				return tileType[lx, ly] == BLOCK;
			}

			/// <summary>
			/// Check if the player body (2 wide, 3 tall) can exist at feet position (feetX, feetY).
			/// Player occupies: (feetX, feetY-2), (feetX, feetY-1), (feetX, feetY),
			///                   (feetX+1, feetY-2), (feetX+1, feetY-1), (feetX+1, feetY)
			/// Grid weight > 0 means passable.
			/// </summary>
			private static bool CanOccupy(byte[,] grid, int lx, int ly, int w, int h)
			{
				// ly = feet y (local). Player body: ly-2 to ly, lx to lx+1
				for (int dx = 0; dx <= 1; dx++)
				{
					for (int dy = -2; dy <= 0; dy++)
					{
						int cx = lx + dx;
						int cy = ly + dy;
						if (cx < 0 || cy < 0 || cx >= w || cy >= h) return false;
						if (grid[cx, cy] == 0) return false;
					}
				}
				return true;
			}

			/// <summary>
			/// Check if player's feet are on ground (block or platform below feet).
			/// </summary>
			private static bool FeetOnGround(byte[,] tileType, int lx, int ly, int w, int h)
			{
				// Check tile below each foot column
				return IsGround(tileType, lx, ly + 1, w, h) || IsGround(tileType, lx + 1, ly + 1, w, h);
			}

			/// <summary>
			/// Check if there's a ceiling directly above the player's head.
			/// </summary>
			private static bool AtCeiling(byte[,] tileType, int lx, int ly, int w, int h)
			{
				// Head is at ly-2, check ly-3
				return IsBlock(tileType, lx, ly - 3, w, h) || IsBlock(tileType, lx + 1, ly - 3, w, h);
			}

			/// <summary>
			/// Main A* pathfinding with JumpLength state encoding.
			/// startX/Y and targetX/Y are in world tile coordinates. Y = feet position.
			/// </summary>
			/// <summary>
			/// Check if player has wings equipped (wingTimeMax > 0).
			/// </summary>
			internal static bool PlayerHasWings()
			{
				var player = Main.LocalPlayer;
				if (player == null || !player.active) return false;
				// wingTimeMax is updated by UpdateEquips each frame
				if (player.wingTimeMax > 0) return true;
				// Fallback: check accessory slots directly for wing items
				for (int i = 3; i < 10; i++)
				{
					var item = player.armor[i];
					if (item != null && !item.IsAir && item.wingSlot > 0)
						return true;
				}
				return false;
			}

			public static PathResult FindPath(int startX, int startY, int targetX, int targetY,
				int range, bool allowDig, int pickPower, int airPenalty = 1, bool hasWings = false)
			{
				int maxJump = CalculateMaxJumpHeight();

				// Grid origin: centered between start and target, clamped to range
				int midX = (startX + targetX) / 2;
				int midY = (startY + targetY) / 2;
				int gridW = range * 2;
				int gridH = range * 2;
				int originX = midX - range;
				int originY = midY - range;

				// Ensure start and target are within grid
				originX = Math.Min(originX, Math.Min(startX, targetX) - 10);
				originY = Math.Min(originY, Math.Min(startY, targetY) - 20);
				int maxNeededX = Math.Max(startX, targetX) + 10;
				int maxNeededY = Math.Max(startY, targetY) + 20;
				gridW = Math.Max(gridW, maxNeededX - originX + 1);
				gridH = Math.Max(gridH, maxNeededY - originY + 1);

				// Cap grid size to prevent excessive memory
				gridW = Math.Min(gridW, 300);
				gridH = Math.Min(gridH, 300);

				// Rebuild origin to keep start/target in bounds
				originX = Math.Max(0, Math.Min(originX, Main.maxTilesX - gridW));
				originY = Math.Max(0, Math.Min(originY, Main.maxTilesY - gridH));

				byte[,] tileTypeGrid;
				var grid = BuildGrid(originX, originY, gridW, gridH, out tileTypeGrid, allowDig, pickPower);

				// Convert to local coords
				int sLx = startX - originX;
				int sLy = startY - originY;
				int tLx = targetX - originX;
				int tLy = targetY - originY;

				// DEBUG: log pathfinding parameters
				ModLoader.GetMod("LumiBridge")?.Logger.Info(
					$"[A*] FindPath start=({startX},{startY}) target=({targetX},{targetY}) " +
					$"origin=({originX},{originY}) grid={gridW}x{gridH} " +
					$"sLocal=({sLx},{sLy}) tLocal=({tLx},{tLy}) " +
					$"allowDig={allowDig} pickPower={pickPower} maxJump={maxJump} airPenalty={airPenalty}");

				// Validate start and target
				if (sLx < 0 || sLy < 0 || sLx >= gridW - 1 || sLy >= gridH)
					return new PathResult { Success = false, Reason = "start_out_of_grid" };
				if (tLx < 0 || tLy < 0 || tLx >= gridW - 1 || tLy >= gridH)
					return new PathResult { Success = false, Reason = "target_out_of_grid" };

				// If target is not occupiable, find nearest occupiable+grounded spot
				if (!CanOccupy(grid, tLx, tLy, gridW, gridH))
				{
					int bestDist = int.MaxValue;
					int bestX = tLx, bestY = tLy;
					bool found = false;
					for (int searchR = 1; searchR <= 10; searchR++)
					{
						for (int sy = tLy - searchR; sy <= tLy + searchR; sy++)
						{
							for (int sx = tLx - searchR; sx <= tLx + searchR; sx++)
							{
								if (sx < 0 || sy < 2 || sx >= gridW - 1 || sy >= gridH) continue;
								if (!CanOccupy(grid, sx, sy, gridW, gridH)) continue;
								if (!FeetOnGround(tileTypeGrid, sx, sy, gridW, gridH)) continue;
								int d = Math.Abs(sx - tLx) + Math.Abs(sy - tLy);
								if (d < bestDist)
								{
									bestDist = d;
									bestX = sx;
									bestY = sy;
									found = true;
								}
							}
						}
						if (found) break;
					}
					if (found)
					{
						ModLoader.GetMod("LumiBridge")?.Logger.Info(
							$"[A*] Target ({tLx},{tLy}) not occupiable, adjusted to ({bestX},{bestY}) dist={bestDist}");
						tLx = bestX;
						tLy = bestY;
					}
					else
					{
						return new PathResult { Success = false, Reason = "target_blocked",
							DebugInfo = $"target ({targetX},{targetY}) not occupiable, no nearby alternative" };
					}
				}

				// Similarly for start
				if (!CanOccupy(grid, sLx, sLy, gridW, gridH))
				{
					int bestDist = int.MaxValue;
					int bestX = sLx, bestY = sLy;
					bool found = false;
					for (int searchR = 1; searchR <= 5; searchR++)
					{
						for (int sy = sLy - searchR; sy <= sLy + searchR; sy++)
						{
							for (int sx = sLx - searchR; sx <= sLx + searchR; sx++)
							{
								if (sx < 0 || sy < 2 || sx >= gridW - 1 || sy >= gridH) continue;
								if (!CanOccupy(grid, sx, sy, gridW, gridH)) continue;
								int d = Math.Abs(sx - sLx) + Math.Abs(sy - sLy);
								if (d < bestDist)
								{
									bestDist = d;
									bestX = sx;
									bestY = sy;
									found = true;
								}
							}
						}
						if (found) break;
					}
					if (found)
					{
						ModLoader.GetMod("LumiBridge")?.Logger.Info(
							$"[A*] Start ({sLx},{sLy}) not occupiable, adjusted to ({bestX},{bestY}) dist={bestDist}");
						sLx = bestX;
						sLy = bestY;
					}
					else
					{
						return new PathResult { Success = false, Reason = "start_blocked",
							DebugInfo = $"start ({startX},{startY}) not occupiable, no nearby alternative" };
					}
				}

				// ── Wing flight mode: simple 8-direction A* without JumpLength ──
				if (hasWings)
				{
					return FindPathFlying(sLx, sLy, tLx, tLy, grid, tileTypeGrid,
						gridW, gridH, originX, originY, allowDig, pickPower);
				}

				// Target must be grounded (arrival requires JumpLength==0)
				if (!FeetOnGround(tileTypeGrid, tLx, tLy, gridW, gridH))
				{
					int bestDist = int.MaxValue;
					int bestX = tLx, bestY = tLy;
					bool found = false;
					for (int searchR = 1; searchR <= 10; searchR++)
					{
						for (int sy = tLy - searchR; sy <= tLy + searchR; sy++)
						{
							for (int sx = tLx - searchR; sx <= tLx + searchR; sx++)
							{
								if (sx < 0 || sy < 2 || sx >= gridW - 1 || sy >= gridH) continue;
								if (!CanOccupy(grid, sx, sy, gridW, gridH)) continue;
								if (!FeetOnGround(tileTypeGrid, sx, sy, gridW, gridH)) continue;
								int d = Math.Abs(sx - tLx) + Math.Abs(sy - tLy);
								if (d < bestDist)
								{
									bestDist = d;
									bestX = sx;
									bestY = sy;
									found = true;
								}
							}
						}
						if (found) break;
					}
					if (found)
					{
						ModLoader.GetMod("LumiBridge")?.Logger.Info(
							$"[A*] Target ({tLx},{tLy}) not grounded, adjusted to ({bestX},{bestY}) dist={bestDist}");
						tLx = bestX;
						tLy = bestY;
					}
				}

				// A* with JumpLength
				var openSet = new SortedSet<(int F, int counter, NodeKey key)>();
				var nodes = new Dictionary<NodeKey, Node>();
				int nodeCounter = 0;

				int startJump = FeetOnGround(tileTypeGrid, sLx, sLy, gridW, gridH) ? 0 : (maxJump * 2);
				var startKey = new NodeKey(sLx, sLy, startJump);
				var startNode = new Node
				{
					X = sLx, Y = sLy, JumpLength = (short)startJump,
					G = 0, F = Heuristic(sLx, sLy, tLx, tLy),
					Parent = null, Status = OPEN
				};
				nodes[startKey] = startNode;
				openSet.Add((startNode.F, nodeCounter++, startKey));

				int steps = 0;

				// Neighbor offsets: right, left, up, down, and diagonals for falling
				int[] dx = { 1, -1, 0, 0, 1, -1 };
				int[] dy = { 0, 0, -1, 1, 1, 1 };

				while (openSet.Count > 0 && steps < MAX_SEARCH_STEPS)
				{
					steps++;
					var (_, _, currentKey) = openSet.Min;
					openSet.Remove(openSet.Min);
					var current = nodes[currentKey];

					if (current.Status == CLOSED) continue;
					current.Status = CLOSED;

					// Arrival check (exact match, must be grounded)
					if (current.X == tLx && current.Y == tLy && current.JumpLength == 0)
					{
						// Reconstruct path
						var rawPath = ReconstructPath(current, originX, originY);
						var waypoints = SimplifyPath(rawPath, originX, originY, tileTypeGrid, gridW, gridH, maxJump);
						return new PathResult
						{
							Success = true,
							Waypoints = waypoints,
							RawLength = rawPath.Count
						};
					}

					bool onGround = FeetOnGround(tileTypeGrid, current.X, current.Y, gridW, gridH);
					bool ceiling = AtCeiling(tileTypeGrid, current.X, current.Y, gridW, gridH);

					// Expand neighbors
					for (int i = 0; i < 6; i++)
					{
						int nx = current.X + dx[i];
						int ny = current.Y + dy[i];

						if (nx < 0 || ny < 2 || nx >= gridW - 1 || ny >= gridH) continue;
						if (!CanOccupy(grid, nx, ny, gridW, gridH)) continue;

						// Calculate new JumpLength
						int newJump = CalculateNewJumpLength(
							current.JumpLength, current.X, current.Y, nx, ny,
							onGround, ceiling, maxJump,
							tileTypeGrid, gridW, gridH);

						if (newJump < 0) continue; // Move not allowed

						// Movement cost: air tiles get airPenalty, dig tiles get base cost 1
						int digCostSum = 0;
						bool anyDig = false;
						for (int bdx = 0; bdx <= 1; bdx++)
						{
							for (int bdy = -2; bdy <= 0; bdy++)
							{
								int bx = nx + bdx;
								int by = ny + bdy;
								if (bx >= 0 && by >= 0 && bx < gridW && by < gridH)
								{
									if (tileTypeGrid[bx, by] == BLOCK && grid[bx, by] > 1)
									{
										digCostSum += grid[bx, by] - 1;
										anyDig = true;
									}
								}
							}
						}
						// If digging through blocks, base cost is 1 (not airPenalty)
						// If walking through air, base cost is airPenalty
						int moveCost = anyDig ? (1 + digCostSum) : airPenalty;

						// Jump cost penalty — heavily penalize deep falls (beyond maxJump)
						// This forces A* to prefer dig paths over air cavities
						int jumpPenalty = newJump / 4;
						int fallDepth = newJump - maxJump * 2; // how far past jump peak
						if (fallDepth > maxJump * 2) // fallen more than maxJump tiles
							jumpPenalty += (fallDepth - maxJump * 2) * 5; // heavy penalty

						int newG = current.G + moveCost + jumpPenalty;
						int newF = newG + Heuristic(nx, ny, tLx, tLy);

						var neighborKey = new NodeKey(nx, ny, newJump);
						if (nodes.TryGetValue(neighborKey, out var existing))
						{
							if (existing.Status == CLOSED || existing.G <= newG) continue;
							// Update existing node
							existing.G = newG;
							existing.F = newF;
							existing.Parent = current;
							existing.Status = OPEN;
							openSet.Add((newF, nodeCounter++, neighborKey));
						}
						else
						{
							var neighbor = new Node
							{
								X = nx, Y = ny, JumpLength = (short)newJump,
								G = newG, F = newF, Parent = current, Status = OPEN
							};
							nodes[neighborKey] = neighbor;
							openSet.Add((newF, nodeCounter++, neighborKey));
						}
					}
				}

				var failReason = steps >= MAX_SEARCH_STEPS ? "search_limit" : "no_path";
				bool dbgStartOccupy = CanOccupy(grid, sLx, sLy, gridW, gridH);
				bool dbgTargetOccupy = CanOccupy(grid, tLx, tLy, gridW, gridH);
				bool dbgStartGround = FeetOnGround(tileTypeGrid, sLx, sLy, gridW, gridH);
				bool dbgTargetGround = FeetOnGround(tileTypeGrid, tLx, tLy, gridW, gridH);
				var debugStr = $"steps={steps} nodes={nodes.Count} open={openSet.Count} " +
					$"startOccupy={dbgStartOccupy} startGround={dbgStartGround} " +
					$"targetOccupy={dbgTargetOccupy} targetGround={dbgTargetGround} " +
					$"grid={gridW}x{gridH} origin=({originX},{originY})";
				ModLoader.GetMod("LumiBridge")?.Logger.Info($"[A*] FAILED {failReason}: {debugStr}");
				return new PathResult
				{
					Success = false,
					Reason = failReason,
					DebugInfo = debugStr
				};
			}

			/// <summary>
			/// Simplified A* for wing flight — 8-direction free movement, no JumpLength.
			/// Flying characters can move freely in any direction through air.
			/// </summary>
			private static PathResult FindPathFlying(int sLx, int sLy, int tLx, int tLy,
				byte[,] grid, byte[,] tileTypeGrid, int gridW, int gridH,
				int originX, int originY, bool allowDig, int pickPower)
			{
				// 8 directions: right, left, up, down, up-right, up-left, down-right, down-left
				int[] dx = { 1, -1, 0, 0, 1, -1, 1, -1 };
				int[] dy = { 0, 0, -1, 1, -1, -1, 1, 1 };

				// State is just (X, Y) — no JumpLength needed for flight
				var openSet = new SortedSet<(int F, int counter, int x, int y)>();
				var gScore = new Dictionary<(int, int), int>();
				var parent = new Dictionary<(int, int), (int, int)?>();
				int nodeCounter = 0;

				var startPos = (sLx, sLy);
				gScore[startPos] = 0;
				parent[startPos] = null;
				openSet.Add((Heuristic(sLx, sLy, tLx, tLy), nodeCounter++, sLx, sLy));

				int steps = 0;

				while (openSet.Count > 0 && steps < MAX_SEARCH_STEPS)
				{
					steps++;
					var (_, _, cx, cy) = openSet.Min;
					openSet.Remove(openSet.Min);

					var curPos = (cx, cy);
					int curG = gScore.GetValueOrDefault(curPos, int.MaxValue);

					// Arrival: within 1 tile of target (no grounded requirement for flying)
					if (Math.Abs(cx - tLx) + Math.Abs(cy - tLy) <= 1)
					{
						// Reconstruct path
						var rawPath = new List<Point>();
						(int, int)? p = curPos;
						while (p.HasValue)
						{
							rawPath.Add(new Point(p.Value.Item1 + originX, p.Value.Item2 + originY));
							p = parent.GetValueOrDefault(p.Value);
						}
						rawPath.Reverse();

						// Simplify: keep direction changes only
						var waypoints = SimplifyFlyingPath(rawPath);
						return new PathResult
						{
							Success = true,
							Waypoints = waypoints,
							RawLength = rawPath.Count
						};
					}

					for (int i = 0; i < 8; i++)
					{
						int nx = cx + dx[i];
						int ny = cy + dy[i];

						if (nx < 0 || ny < 0 || nx >= gridW - 1 || ny >= gridH) continue;
						if (!CanOccupy(grid, nx, ny, gridW, gridH)) continue;

						// Cost: digging or air movement
						int digCostSum = 0;
						bool anyDig = false;
						for (int bdx = 0; bdx <= 1; bdx++)
						{
							for (int bdy = -2; bdy <= 0; bdy++)
							{
								int bx = nx + bdx;
								int by = ny + bdy;
								if (bx >= 0 && by >= 0 && bx < gridW && by < gridH)
								{
									if (tileTypeGrid[bx, by] == BLOCK && grid[bx, by] > 1)
									{
										digCostSum += grid[bx, by] - 1;
										anyDig = true;
									}
								}
							}
						}
						// Flying: air is cheap (cost 1), dig has normal cost
						int moveCost = anyDig ? (1 + digCostSum) : 1;
						// Diagonal moves cost slightly more (√2 ≈ 1.4)
						if (dx[i] != 0 && dy[i] != 0) moveCost += 1;

						int newG = curG + moveCost;
						var nPos = (nx, ny);

						if (newG < gScore.GetValueOrDefault(nPos, int.MaxValue))
						{
							gScore[nPos] = newG;
							parent[nPos] = curPos;
							int newF = newG + Heuristic(nx, ny, tLx, tLy);
							openSet.Add((newF, nodeCounter++, nx, ny));
						}
					}
				}

				var failReason = steps >= MAX_SEARCH_STEPS ? "search_limit" : "no_path";
				return new PathResult
				{
					Success = false,
					Reason = failReason,
					DebugInfo = $"flying steps={steps} grid={gridW}x{gridH}"
				};
			}

			/// <summary>
			/// Simplify flying path: keep only direction changes.
			/// </summary>
			private static List<Point> SimplifyFlyingPath(List<Point> rawPath)
			{
				if (rawPath.Count <= 2) return rawPath;

				var result = new List<Point> { rawPath[0] };
				for (int i = 1; i < rawPath.Count - 1; i++)
				{
					var prev = rawPath[i - 1];
					var curr = rawPath[i];
					var next = rawPath[i + 1];
					// Keep if direction changes
					if (curr.X - prev.X != next.X - curr.X || curr.Y - prev.Y != next.Y - curr.Y)
						result.Add(curr);
				}
				result.Add(rawPath[rawPath.Count - 1]);
				return result;
			}

			/// <summary>
			/// Calculate new JumpLength based on movement direction and current state.
			/// Returns -1 if the move is not allowed.
			/// </summary>
			private static int CalculateNewJumpLength(int currentJump, int oldX, int oldY,
				int newX, int newY, bool onGround, bool atCeiling, int maxJump,
				byte[,] tileType, int gridW, int gridH)
			{
				bool newOnGround = FeetOnGround(tileType, newX, newY, gridW, gridH);
				bool horizontalMove = newX != oldX;
				bool goingUp = newY < oldY;
				bool goingDown = newY > oldY;

				// Landing on ground resets jump
				bool blockedOneWayLanding = false;
				if (newOnGround && !goingUp)
				{
					// For OneWay platforms: can only land if already grounded (walking onto it)
					// or in falling phase (fallen past peak from above).
					// Cannot land while ascending through platform from below.
					// Threshold: maxJump*2 = peak, +2 = started falling (~1 tile).
					if (currentJump > 0 && currentJump < maxJump * 2 + 2)
					{
						// Check if ground is purely OneWay (no Block support)
						bool hasBlock = IsBlock(tileType, newX, newY + 1, gridW, gridH)
							|| IsBlock(tileType, newX + 1, newY + 1, gridW, gridH);
						if (!hasBlock)
						{
							// OneWay only + not clearly falling = treat as air
							// (flag prevents onGround shortcut from re-allowing landing)
							blockedOneWayLanding = true;
						}
						else
						{
							return 0; // Has solid block support, can land
						}
					}
					else
					{
						return 0; // Grounded (0) or clearly falling, can always land
					}
				}

				// Odd JumpLength = ascending phase, no horizontal movement allowed
				if (currentJump > 0 && currentJump % 2 != 0 && horizontalMove)
					return -1;

				// Already at max jump height, cannot go up
				if (currentJump >= maxJump * 2 && goingUp)
					return -1;

				// Falling: restrict horizontal movement frequency (air control is limited)
				int newJump;

				if (atCeiling && goingUp)
				{
					// Hit ceiling — force into falling
					return -1; // Can't go up into ceiling
				}

				if (atCeiling && !goingDown)
				{
					// At ceiling, moving horizontally — start falling
					newJump = Math.Max(maxJump * 2 + 1, currentJump + 1);
				}
				else if (goingUp)
				{
					// Ascending
					if (currentJump < 2)
						newJump = 3; // First jump step
					else if (currentJump % 2 == 0)
						newJump = currentJump + 2; // Continue ascending
					else
						newJump = currentJump + 1; // Horizontal step during ascent
				}
				else if (goingDown)
				{
					// Descending
					if (currentJump % 2 == 0)
						newJump = Math.Max(maxJump * 2, currentJump + 2);
					else
						newJump = Math.Max(maxJump * 2, currentJump + 1);
				}
				else
				{
					// Horizontal in air
					if (onGround && !blockedOneWayLanding)
						newJump = 0; // Walking on ground
					else
						newJump = currentJump + 1;
				}

				// Falling horizontal restriction: limit air control during descent
				if (newJump >= maxJump * 2 + 4 && horizontalMove)
				{
					// Allow horizontal moves every few frames (simulate limited air control)
					if ((newJump - (maxJump * 2 + 4)) % 6 != 2)
						return -1;
				}

				return newJump;
			}

			private static int Heuristic(int x1, int y1, int x2, int y2)
			{
				return Math.Abs(x1 - x2) + Math.Abs(y1 - y2);
			}

			private static List<Point> ReconstructPath(Node end, int originX, int originY)
			{
				var path = new List<Point>();
				var node = end;
				while (node != null)
				{
					path.Add(new Point(node.X + originX, node.Y + originY));
					node = node.Parent;
				}
				path.Reverse();
				return path;
			}

			/// <summary>
			/// Simplify path: keep only key waypoints (direction changes, jumps, landings, platforms).
			/// </summary>
			private static List<Point> SimplifyPath(List<Point> rawPath, int originX, int originY,
				byte[,] tileType, int gridW, int gridH, int maxJump)
			{
				if (rawPath.Count <= 2) return rawPath;

				var result = new List<Point> { rawPath[0] }; // Always keep start

				for (int i = 1; i < rawPath.Count - 1; i++)
				{
					var prev = rawPath[i - 1];
					var curr = rawPath[i];
					var next = rawPath[i + 1];

					// Direction change in X
					int dxPrev = curr.X - prev.X;
					int dxNext = next.X - curr.X;
					if (dxPrev != dxNext)
					{
						result.Add(curr);
						continue;
					}

					// Direction change in Y
					int dyPrev = curr.Y - prev.Y;
					int dyNext = next.Y - curr.Y;
					if (dyPrev != dyNext)
					{
						result.Add(curr);
						continue;
					}

					// Standing on OneWay platform
					int lx = curr.X - originX;
					int ly = curr.Y - originY;
					if (lx >= 0 && ly >= 0 && lx < gridW && ly + 1 < gridH)
					{
						if (tileType[lx, ly + 1] == ONE_WAY || (lx + 1 < gridW && tileType[lx + 1, ly + 1] == ONE_WAY))
						{
							result.Add(curr);
							continue;
						}
					}

					// Preserve grounded waypoints if removing would create vertical gap > maxJump
					// (prevents SimplifyPath from generating unexecutable waypoint sequences)
					var lastKept = result[result.Count - 1];
					int vGap = Math.Abs(lastKept.Y - next.Y);
					if (vGap > maxJump)
					{
						// Check if current point is grounded (landing spot)
						if (FeetOnGround(tileType, lx, ly, gridW, gridH))
						{
							result.Add(curr);
							continue;
						}
					}
				}

				result.Add(rawPath[rawPath.Count - 1]); // Always keep end
				return result;
			}
		}

		// ══════════════════════════════════════════════════════════════
		// Navigator — executes A* path frame by frame
		// ══════════════════════════════════════════════════════════════

		private class Navigator
		{
			public bool Active { get; private set; }

			private List<Point> _waypoints;
			private int _waypointIndex;
			private int _targetX, _targetY;
			private int _effectiveTargetX, _effectiveTargetY; // last waypoint pos for arrival
			private bool _allowDig;
			private int _pickPower;
			private int _baseAirPenalty = 1;
			private bool _hasWings;

			// Jump state
			private int _jumpFramesRemaining;

			// Track position at last repath to detect progress
			private int _repathX, _repathY;

			// Stuck detection
			private int _lastTileX, _lastTileY;
			private int _stuckFrames;
			private int _retryCount;
			private const int STUCK_THRESHOLD = 90;       // ~1.5 seconds (no dig)
			private const int STUCK_THRESHOLD_DIG = 150;  // ~2.5 seconds (while digging — one block takes ~2s)
			private const int MAX_RETRIES = 0;            // no repath — fail immediately, let Python decide next move

			// Waypoint-level progress tracking (detect bouncing without tile-level stuck)
			private int _lastProgressWpIndex;
			private int _noProgressFrames;
			private int _repathWpIndex;
			private const int NO_PROGRESS_THRESHOLD = 240;  // ~4 seconds of no waypoint advancement

			// Dig failure tracking — detect unmineable tiles (anchored objects above, altars, etc.)
			private int _digTargetX = -1, _digTargetY = -1;
			private int _digFrames;
			private const int DIG_FAIL_THRESHOLD = 300;  // ~5 seconds on same tile = give up
			private HashSet<(int, int)> _unmineableTiles = new();

			// Progress reporting
			private int _reportCounter;
			private const int REPORT_INTERVAL = 30;  // every 0.5s

			/// <summary>
			/// Calculate how many frames to hold jump key to reach a given height in tiles.
			/// Simulates the jump: powered phase at jumpSpeed, then coasting with gravity.
			/// Returns an array indexed by tile height: JumpFrames[h] = frames needed for h tiles.
			/// </summary>
			private static int[] BuildJumpFrameTable()
			{
				var player = Main.LocalPlayer;
				float jumpSpeed = Player.jumpSpeed > 0f ? Player.jumpSpeed : 5.01f;
				int jumpHeight = Player.jumpHeight > 0 ? Player.jumpHeight : 15;
				float gravity = Player.defaultGravity > 0f ? Player.defaultGravity : 0.4f;

				int maxJump = Pathfinder.CalculateMaxJumpHeight();
				int[] table = new int[maxJump + 1];
				// table[0] = 0 (no jump needed)

				// Simulate pressing jump for increasing frame counts, record max height reached
				for (int holdFrames = 1; holdFrames <= jumpHeight + 30; holdFrames++)
				{
					float y = 0f; // pixels upward
					float vel = 0f;
					for (int f = 0; f < holdFrames + 60; f++) // simulate enough frames to reach apex
					{
						if (f < holdFrames && f < jumpHeight)
							vel = jumpSpeed; // powered phase: constant upward speed
						else
							vel -= gravity;  // coasting/falling

						y += vel;
						if (vel <= 0f) break; // reached apex
					}

					int tilesReached = (int)(y / 16f);
					// Fill table: first holdFrames that reaches each tile height
					for (int h = 1; h <= Math.Min(tilesReached, maxJump); h++)
					{
						if (table[h] == 0)
							table[h] = holdFrames;
					}

					if (tilesReached >= maxJump) break; // all entries filled
				}

				// Fill any remaining entries with max jumpHeight
				for (int h = 1; h <= maxJump; h++)
				{
					if (table[h] == 0)
						table[h] = jumpHeight;
				}

				return table;
			}

			private int[] _jumpFrameTable;
			private int[] GetJumpFrameTable()
			{
				// Rebuild once per navigation start (equipment doesn't change mid-navigation)
				return _jumpFrameTable ??= BuildJumpFrameTable();
			}

			public void Start(List<Point> waypoints, int targetX, int targetY, bool allowDig, int pickPower, int baseAirPenalty = 1, bool hasWings = false)
			{
				_waypoints = waypoints;
				_waypointIndex = 0;
				_targetX = targetX;
				_targetY = targetY;
				var lastWp = _waypoints[_waypoints.Count - 1];
				_effectiveTargetX = lastWp.X;
				_effectiveTargetY = lastWp.Y;
				_allowDig = allowDig;
				_pickPower = pickPower;
				_baseAirPenalty = baseAirPenalty;
				_hasWings = hasWings;
				_jumpFrameTable = hasWings ? null : BuildJumpFrameTable();
				_jumpFramesRemaining = 0;
				_stuckFrames = 0;
				_retryCount = 0;
				_reportCounter = 0;
				_lastTileX = -1;
				_lastTileY = -1;
				_repathX = -1;
				_repathY = -1;
				_lastProgressWpIndex = 0;
				_digTargetX = -1;
				_digTargetY = -1;
				_digFrames = 0;
				_unmineableTiles.Clear();
				_noProgressFrames = 0;
				_repathWpIndex = -1;
				Active = true;
			}

			public void Cancel()
			{
				Active = false;
				_waypoints = null;
			}

			public void Update(LumiBridgeSystem bridge)
			{
				if (!Active || _waypoints == null || _waypoints.Count == 0) return;

				var player = Main.LocalPlayer;
				if (player == null || !player.active || player.dead)
				{
					Finish(bridge, "stuck", "player_dead");
					return;
				}

				int feetX = (int)(player.Center.X / 16f);
				int feetY = (int)(player.position.Y / 16f) + 2;


				// ── Arrival check (final target) ──
				// Wings: don't require grounded (can arrive in air). Ground: must be grounded.
				bool grounded = player.velocity.Y == 0f;
				bool arrivalOk = _hasWings || grounded;
				if (arrivalOk && Math.Abs(feetX - _effectiveTargetX) + Math.Abs(feetY - _effectiveTargetY) <= 1)
				{
					Finish(bridge, "arrived", null);
					return;
				}

				// ── Advance waypoint if reached ──
				if (_waypointIndex < _waypoints.Count)
				{
					var wp = _waypoints[_waypointIndex];
					// Wings: lenient waypoint reach (2 tile tolerance, no grounded requirement)
					// Ground: last waypoint requires grounded + exact match
					bool isLast = _waypointIndex == _waypoints.Count - 1;
					bool reached;
					if (_hasWings)
						reached = Math.Abs(feetX - wp.X) <= 1 && Math.Abs(feetY - wp.Y) <= 1;
					else
						reached = isLast
							? (grounded && Math.Abs(feetX - wp.X) <= 1 && Math.Abs(feetY - wp.Y) <= 1)
							: (Math.Abs(feetX - wp.X) <= 1 && Math.Abs(feetY - wp.Y) <= 1);
					if (reached)
					{
						_waypointIndex++;
						_stuckFrames = 0;
						if (_waypointIndex >= _waypoints.Count)
						{
							// Past last waypoint, check if arrived (relaxed)
							if (grounded && Math.Abs(feetX - _effectiveTargetX) + Math.Abs(feetY - _effectiveTargetY) <= 1)
							{
								Finish(bridge, "arrived", null);
								return;
							}
							// Don't repath immediately; fall through to walk toward target
						}
					}
				}

				// When past all waypoints, use final target for movement
				// (stuck detection will handle repath if needed)

				// Waypoint-level progress check (detect bouncing)
				// Use longer threshold when actively digging (need time for pickaxe to break blocks)
				if (_waypointIndex > _lastProgressWpIndex)
				{
					_lastProgressWpIndex = _waypointIndex;
					_noProgressFrames = 0;
				}
				else
				{
					_noProgressFrames++;
					bool actuallyDigging = _digTargetX >= 0 && _digFrames > 0;
					int wpThreshold = actuallyDigging ? STUCK_THRESHOLD_DIG : NO_PROGRESS_THRESHOLD;
					if (_noProgressFrames >= wpThreshold)
					{
						_noProgressFrames = 0;
						Repath(bridge, feetX, feetY, "no_wp_progress");
						return;
					}
				}

				var target = _waypointIndex < _waypoints.Count
					? _waypoints[_waypointIndex]
					: new Point(_effectiveTargetX, _effectiveTargetY);

				// ── Clear controls ──
				ResetMovement();
				ControlUseItem = false;
				TargetTileX = -1;
				TargetTileY = -1;

				// ── Stuck detection ──
				if (feetX == _lastTileX && feetY == _lastTileY)
				{
					_stuckFrames++;
					// Only use long threshold when ACTUALLY swinging pickaxe at a tile
					bool actuallyDigging = _digTargetX >= 0 && _digFrames > 0;
					int threshold = actuallyDigging ? STUCK_THRESHOLD_DIG : STUCK_THRESHOLD;
					if (_stuckFrames >= threshold)
					{
						Repath(bridge, feetX, feetY);
						return;
					}
				}
				else
				{
					_stuckFrames = 0;
					_lastTileX = feetX;
					_lastTileY = feetY;
				}

				// ── Wing flight mode ──
				if (_hasWings)
				{
					// Simple flight control: hold Jump to fly up, release to fall/glide
					if (target.Y < feetY)
						ControlJump = true;  // target above → fly up
					else if (target.Y == feetY && !grounded)
						{ /* same height in air → don't press jump, glide */ }
					// target below → don't press jump (fall)

					// Horizontal movement
					if (target.X < feetX)
						ControlLeft = true;
					else if (target.X > feetX)
						ControlRight = true;

					// Dig through blocks if needed (with unmineable detection)
					if (_allowDig)
					{
						// Horizontal blocking — use actual body edge to find adjacent tile
						if (target.X != feetX)
						{
							int lookDir = target.X > feetX ? 1 : -1;
							int lookX;
							if (lookDir > 0)
								lookX = (int)((player.position.X + player.width) / 16f);
							else
								lookX = (int)(player.position.X / 16f) - 1;
							for (int dy = -2; dy <= 0; dy++)
							{
								int ly = feetY + dy;
								if (lookX >= 0 && lookX < Main.maxTilesX && ly >= 0 && ly < Main.maxTilesY)
								{
									var tile = Main.tile[lookX, ly];
									if (tile.HasTile && Main.tileSolid[(int)tile.TileType] && !Main.tileSolidTop[(int)tile.TileType])
									{
										if (TrySetDigTarget(lookX, ly, player))
											break;  // digging this tile, done
										// else: tile unmineable, try next row
									}
								}
							}
						}
						// Vertical blocking (above) — scan multiple rows to find ceiling
						if (target.Y < feetY)
						{
							bool dug = false;
							for (int aboveY = feetY - 3; aboveY >= feetY - 5 && !dug; aboveY--)
							{
								for (int ddx = 0; ddx <= 1; ddx++)
								{
									int cx = feetX + ddx;
									if (cx >= 0 && cx < Main.maxTilesX && aboveY >= 0 && aboveY < Main.maxTilesY)
									{
										var tile = Main.tile[cx, aboveY];
										if (tile.HasTile && Main.tileSolid[(int)tile.TileType] && !Main.tileSolidTop[(int)tile.TileType])
										{
											if (TrySetDigTarget(cx, aboveY, player))
											{ dug = true; break; }
										}
									}
								}
							}
							// If stuck going up and can't dig, jitter horizontally to find opening
							if (!dug && _stuckFrames > 60)
							{
								if ((_stuckFrames / 30) % 2 == 0)
									ControlLeft = true;
								else
									ControlRight = true;
							}
						}
						// Vertical blocking (below) — must clear BOTH columns for player to fall
						if (target.Y > feetY)
						{
							int belowY = feetY + 1;
							for (int ddx = 0; ddx <= 1; ddx++)
							{
								int cx = feetX + ddx;
								if (cx >= 0 && cx < Main.maxTilesX && belowY >= 0 && belowY < Main.maxTilesY)
								{
									var tile = Main.tile[cx, belowY];
									if (tile.HasTile && Main.tileSolid[(int)tile.TileType] && !Main.tileSolidTop[(int)tile.TileType])
									{
										if (TrySetDigTarget(cx, belowY, player))
											break;  // digging this tile, come back for the other next frame
									}
								}
							}
						}
					}

					// Progress report (reuse same counter)
					_reportCounter++;
					if (_reportCounter >= REPORT_INTERVAL)
					{
						_reportCounter = 0;
						float progress = _waypoints.Count > 1
							? (float)_waypointIndex / (_waypoints.Count - 1) : 1f;
						bridge.Send(new { type = "nav_status", status = "moving",
							progress = MathF.Round(progress, 2),
							x = feetX, y = feetY, mode = "flying",
							waypointIndex = _waypointIndex,
							waypointsTotal = _waypoints.Count,
							targetWpX = target.X, targetWpY = target.Y,
							wingTime = player.wingTime, wingTimeMax = player.wingTimeMax });
					}
					return; // skip normal ground-based movement logic
				}

				// ── Drop through OneWay platform ──
				if (target.Y > feetY)
				{
					// Check if standing on a platform
					int checkY = feetY + 1;
					if (checkY >= 0 && checkY < Main.maxTilesY)
					{
						bool onPlatform = false;
						for (int dx = 0; dx <= 1; dx++)
						{
							int cx = feetX + dx;
							if (cx >= 0 && cx < Main.maxTilesX)
							{
								var tile = Main.tile[cx, checkY];
								if (tile.HasTile && Main.tileSolidTop[(int)tile.TileType])
								{
									onPlatform = true;
									break;
								}
							}
						}
						if (onPlatform)
						{
							ControlDown = true;
							return; // Just press down this frame
						}
					}
				}

				// ── Walk off ledge to descend ──
				// Target is below, not on a platform — walk sideways to find edge to fall from
				// Also triggers when standing on block edge (both feetX tiles are air below)
				bool feetOnEdge = false;
				if (grounded && feetY + 1 < Main.maxTilesY)
				{
					bool t0Solid = feetX >= 0 && feetX < Main.maxTilesX && Main.tile[feetX, feetY + 1].HasTile && Main.tileSolid[(int)Main.tile[feetX, feetY + 1].TileType];
					bool t1Solid = feetX + 1 >= 0 && feetX + 1 < Main.maxTilesX && Main.tile[feetX + 1, feetY + 1].HasTile && Main.tileSolid[(int)Main.tile[feetX + 1, feetY + 1].TileType];
					feetOnEdge = !t0Solid && !t1Solid;
				}
				if (target.Y > feetY && grounded && (_stuckFrames > 15 || feetOnEdge))
				{
					// Check which side has open air below (can fall there)
					bool openRight = true, openLeft = true;
					int belowY = feetY + 1;
					if (belowY < Main.maxTilesY)
					{
						// Check 1 tile to the right of player body (player is 2 wide: feetX, feetX+1)
						int rx = feetX + 2;
						if (rx >= 0 && rx < Main.maxTilesX)
						{
							var rt = Main.tile[rx, belowY];
							if (rt.HasTile && Main.tileSolid[(int)rt.TileType]) openRight = false;
						}
						// Check 1 tile to the left
						int lx = feetX - 1;
						if (lx >= 0 && lx < Main.maxTilesX)
						{
							var lt = Main.tile[lx, belowY];
							if (lt.HasTile && Main.tileSolid[(int)lt.TileType]) openLeft = false;
						}
					}
					// Prefer direction toward target X, otherwise toward open air
					if (target.X > feetX && openRight) ControlRight = true;
					else if (target.X < feetX && openLeft) ControlLeft = true;
					else if (openRight) ControlRight = true;
					else if (openLeft) ControlLeft = true;
					else { ControlRight = true; } // fallback: just try right
					return;
				}

				// ── Unreachable waypoint detection ──
				if (target.Y < feetY && grounded)
				{
					int maxJumpHeight = Pathfinder.CalculateMaxJumpHeight();
					int heightNeeded = feetY - target.Y;
					if (heightNeeded > maxJumpHeight)
					{
						// Waypoint is above max jump range — repath (A* should find dig route)
						Repath(bridge, feetX, feetY, "unreachable_wp");
						return;
					}
				}

				// ── Jump logic ──
				if (target.Y < feetY && player.velocity.Y == 0f)
				{
					// Need to go up — start a jump
					var jft = GetJumpFrameTable();
					int heightDiff = feetY - target.Y;

					// Check if target is on a OneWay platform — need extra height
					// to clear the platform and land from above (can't land by jumping through)
					bool targetOnPlatform = false;
					int belowTargetY = target.Y + 1;
					if (belowTargetY >= 0 && belowTargetY < Main.maxTilesY)
					{
						for (int dx = 0; dx <= 1; dx++)
						{
							int cx = feetX + dx;
							if (cx >= 0 && cx < Main.maxTilesX)
							{
								var t = Main.tile[cx, belowTargetY];
								if (t.HasTile && Main.tileSolidTop[(int)t.TileType])
								{
									targetOnPlatform = true;
									break;
								}
							}
						}
					}
					if (targetOnPlatform)
						heightDiff += 2; // Jump 2 tiles higher to clear and land from above

					int idx = Math.Min(heightDiff, jft.Length - 1);
					_jumpFramesRemaining = jft[idx];
				}

				if (_jumpFramesRemaining > 0)
				{
					ControlJump = true;
					_jumpFramesRemaining--;
				}

				// ── Horizontal movement ──
				if (target.X < feetX)
					ControlLeft = true;
				else if (target.X > feetX)
					ControlRight = true;

				// ── Dig through blocks ──
				if (_allowDig)
				{
					bool blocked = false;

					// Only check horizontal blocking when target requires horizontal movement
					if (target.X != feetX)
					{
						int lookDir = target.X > feetX ? 1 : -1;
						int lookX;
						if (lookDir > 0)
							lookX = (int)((player.position.X + player.width) / 16f); // tile at body right edge
						else
							lookX = (int)(player.position.X / 16f) - 1; // tile left of body left edge

						for (int dy = -2; dy <= 0; dy++)
						{
							int ly = feetY + dy;
							if (lookX >= 0 && lookX < Main.maxTilesX && ly >= 0 && ly < Main.maxTilesY)
							{
								var tile = Main.tile[lookX, ly];
								if (tile.HasTile && Main.tileSolid[(int)tile.TileType] && !Main.tileSolidTop[(int)tile.TileType])
								{
									blocked = TrySetDigTarget(lookX, ly, player);
									if (blocked) break;
								}
							}
						}
					}

					// Vertical blocking (need to dig down)
					// Check feetX, feetX+1 first; if both air, check feetX-1, feetX+2 (edge standing)
					if (!blocked && target.Y > feetY)
					{
						int belowY = feetY + 1;
						int[] checkDxs = new int[] { 0, 1, -1, 2 };
						foreach (int dx in checkDxs)
						{
							int cx = feetX + dx;
							if (cx >= 0 && cx < Main.maxTilesX && belowY >= 0 && belowY < Main.maxTilesY)
							{
								var tile = Main.tile[cx, belowY];
								if (tile.HasTile && Main.tileSolid[(int)tile.TileType] && !Main.tileSolidTop[(int)tile.TileType])
								{
									if (TrySetDigTarget(cx, belowY, player))
										break;
								}
							}
						}
					}

					// Dig above (need to jump through ceiling)
					// Check edge tiles too (feetX-1, feetX+2)
					if (!blocked && target.Y < feetY)
					{
						int aboveY = feetY - 3;
						int[] aboveDxs = new int[] { 0, 1, -1, 2 };
						foreach (int dx in aboveDxs)
						{
							int cx = feetX + dx;
							if (cx >= 0 && cx < Main.maxTilesX && aboveY >= 0 && aboveY < Main.maxTilesY)
							{
								var tile = Main.tile[cx, aboveY];
								if (tile.HasTile && Main.tileSolid[(int)tile.TileType] && !Main.tileSolidTop[(int)tile.TileType])
								{
									if (TrySetDigTarget(cx, aboveY, player))
										break;
								}
							}
						}
					}
				}

				// ── Progress report ──
				_reportCounter++;
				if (_reportCounter >= REPORT_INTERVAL)
				{
					_reportCounter = 0;
					float progress = _waypoints.Count > 1
						? (float)_waypointIndex / (_waypoints.Count - 1)
						: 1f;

					// Dig diagnostics
					object digInfo = null;
					if (_allowDig && target.Y > feetY)
					{
						int belowY = feetY + 1;
						int t0 = -1, t1 = -1;
						bool s0 = false, s1 = false;
						if (feetX >= 0 && feetX < Main.maxTilesX && belowY < Main.maxTilesY)
						{
							var tile0 = Main.tile[feetX, belowY];
							t0 = tile0.HasTile ? (int)tile0.TileType : -1;
							s0 = tile0.HasTile && Main.tileSolid[t0];
						}
						if (feetX + 1 >= 0 && feetX + 1 < Main.maxTilesX && belowY < Main.maxTilesY)
						{
							var tile1 = Main.tile[feetX + 1, belowY];
							t1 = tile1.HasTile ? (int)tile1.TileType : -1;
							s1 = tile1.HasTile && Main.tileSolid[t1];
						}
						digInfo = new {
							belowY,
							tile0Type = t0, tile0Solid = s0,
							tile1Type = t1, tile1Solid = s1,
							useItem = ControlUseItem,
							digTargetX = TargetTileX, digTargetY = TargetTileY,
							selectedItem = player.selectedItem,
							itemName = player.inventory[player.selectedItem]?.Name ?? "none",
							stuckFrames = _stuckFrames
						};
					}

					// Jump diagnostics
					object jumpInfo = null;
					if (target.Y < feetY || _jumpFramesRemaining > 0 || player.velocity.Y != 0f)
					{
						jumpInfo = new {
							velY = MathF.Round(player.velocity.Y, 2),
							grounded,
							jumpFrames = _jumpFramesRemaining,
							ctrlJump = ControlJump,
							ctrlLeft = ControlLeft,
							ctrlRight = ControlRight,
							targetAbove = target.Y < feetY
						};
					}

					bridge.Send(new { type = "nav_status", status = "moving",
						progress = MathF.Round(progress, 2),
						x = feetX, y = feetY,
						waypointIndex = _waypointIndex,
						waypointsTotal = _waypoints.Count,
						targetWpX = target.X, targetWpY = target.Y,
						dig = digInfo,
						jump = jumpInfo });
				}
			}

			/// <summary>
			/// Check if a tile can actually be mined — returns false for tiles anchored by
			/// trees, chests, demon/crimson altars, or already marked unmineable.
			/// Also tracks dig duration and marks tiles as unmineable after timeout.
			/// </summary>
			private bool TrySetDigTarget(int tx, int ty, Player player)
			{
				if (_unmineableTiles.Contains((tx, ty)))
					return false;

				// Check tile above for common anchor types that prevent mining
				if (ty > 0)
				{
					var above = Main.tile[tx, ty - 1];
					if (above.HasTile)
					{
						int tt = (int)above.TileType;
						// Trees (5=tree, 596=gemTree, 616=VanityTreeSakura, 634=VanityTreeWillow, 323=palmTree)
						// Chests (21, 467), Demon/Crimson Altars (26)
						// Cactus (80), Large piles (186,187,188), Tall plants (227)
						if (tt == 5 || tt == 596 || tt == 616 || tt == 634 || tt == 323
							|| tt == 21 || tt == 467 || tt == 26
							|| tt == 80)
						{
							_unmineableTiles.Add((tx, ty));
							return false;
						}
					}
				}

				// Track how long we've been hitting this tile
				if (tx == _digTargetX && ty == _digTargetY)
				{
					_digFrames++;
					if (_digFrames >= DIG_FAIL_THRESHOLD)
					{
						// Been hitting this tile too long — probably unmineable
						_unmineableTiles.Add((tx, ty));
						_digTargetX = -1;
						_digTargetY = -1;
						_digFrames = 0;
						return false;
					}
				}
				else
				{
					// New dig target
					_digTargetX = tx;
					_digTargetY = ty;
					_digFrames = 0;
				}

				if (!SelectBestPickaxe(player))
					return false;

				ControlUseItem = true;
				TargetTileX = tx;
				TargetTileY = ty;
				return true;
			}

			private void Repath(LumiBridgeSystem bridge, int feetX, int feetY, string triggerReason = "stuck_frames")
			{
				// Use waypoint progress to detect real advancement
				// (position change during bouncing is fake progress)
				if (_waypointIndex > _repathWpIndex)
				{
					// Advanced to new waypoint since last repath — real progress
					_retryCount = 0;
				}
				else
				{
					_retryCount++;
				}
				_repathWpIndex = _waypointIndex;
				_repathX = feetX;
				_repathY = feetY;

				if (_retryCount > MAX_RETRIES)
				{
					Finish(bridge, "stuck", "max_retries");
					return;
				}

				// Re-run pathfinding from current position
				// Progressive dig preference: each retry makes digging cheaper
				// so A* prefers reliable dig paths over risky narrow passages
				int adjustedPickPower = _pickPower;
				if (_retryCount > 0 && _allowDig && _pickPower > 0)
					adjustedPickPower = _pickPower * (1 << _retryCount); // 2x, 4x, 8x
				// Air penalty: if trigger was unreachable_wp, air paths failed
				// → heavily penalize air movement to force dig paths
				int airPenalty = _baseAirPenalty;
				if (triggerReason == "unreachable_wp" && _allowDig)
					airPenalty = Math.Max(_baseAirPenalty, 3 + _retryCount * 3); // 3x, 6x, 9x, but at least baseAirPenalty
				var result = Pathfinder.FindPath(feetX, feetY, _targetX, _targetY,
					100, _allowDig, adjustedPickPower, airPenalty, _hasWings);

				if (result.Success && result.Waypoints.Count > 0)
				{
					_waypoints = result.Waypoints;
					_waypointIndex = 0;
					var lastWp = _waypoints[_waypoints.Count - 1];
					_effectiveTargetX = lastWp.X;
					_effectiveTargetY = lastWp.Y;
					// effectiveTarget = A*'s last waypoint (trusted, A* already adjusts to nearest ground)
					_stuckFrames = 0;
					bridge.Send(new { type = "nav_status", status = "repath",
						retry = _retryCount, waypoints = result.Waypoints.Count,
						reason = triggerReason });
				}
				else
				{
					Finish(bridge, "stuck", result.Reason ?? "no_path");
				}
			}

			private void Finish(LumiBridgeSystem bridge, string status, string reason)
			{
				Active = false;
				_waypoints = null;
				ResetMovement();
				ControlUseItem = false;
				TargetTileX = -1;
				TargetTileY = -1;

				if (reason != null)
					bridge.Send(new { type = "nav_status", status, reason });
				else
					bridge.Send(new { type = "nav_status", status });
			}

			private static bool SelectBestPickaxe(Player player)
			{
				int bestSlot = -1;
				int bestPick = 0;
				for (int i = 0; i < 10; i++) // hotbar only
				{
					var item = player.inventory[i];
					if (item != null && !item.IsAir && item.pick > bestPick)
					{
						bestPick = item.pick;
						bestSlot = i;
					}
				}
				if (bestSlot >= 0)
				{
					player.selectedItem = bestSlot;
					return true;
				}
				return false;
			}
		}

		private void PushState(bool force = false)
		{
			var player = Main.LocalPlayer;
			if (player == null || !player.active) return;

			int hp = player.statLife;
			float px = player.position.X;
			float py = player.position.Y;

			// Only push on change or forced
			if (!force && hp == _lastHealth
				&& Math.Abs(px - _lastPosX) < 1f
				&& Math.Abs(py - _lastPosY) < 1f)
				return;

			_lastHealth = hp;
			_lastPosX = px;
			_lastPosY = py;

			// Build inventory summary (first 10 hotbar slots)
			var hotbar = new List<object>();
			for (int i = 0; i < 10; i++)
			{
				var item = player.inventory[i];
				if (item != null && !item.IsAir)
				{
					hotbar.Add(new
					{
						slot = i,
						name = item.Name,
						id = item.type,
						stack = item.stack,
						pick = item.pick,
						axe = item.axe,
						hammer = item.hammer,
						damage = item.damage,
						createTile = item.createTile,
						createWall = item.createWall
					});
				}
			}

			// Nearby NPCs
			var nearbyNpcs = new List<object>();
			for (int i = 0; i < Main.maxNPCs; i++)
			{
				var npc = Main.npc[i];
				if (npc.active && npc.Distance(player.Center) < 800f)
				{
					nearbyNpcs.Add(new
					{
						name = npc.FullName,
						id = npc.type,
						life = npc.life,
						lifeMax = npc.lifeMax,
						x = npc.position.X,
						y = npc.position.Y,
						friendly = npc.friendly,
						townNPC = npc.townNPC,
						damage = npc.damage,
						boss = npc.boss
					});
				}
			}

			// Sample brightness around player (3x3 grid at player center)
			int ptx = (int)(player.Center.X / 16f);
			int pty = (int)(player.Center.Y / 16f);
			float brightnessSum = 0f;
			int samples = 0;
			for (int bx = ptx - 1; bx <= ptx + 1; bx++)
			{
				for (int by = pty - 1; by <= pty + 1; by++)
				{
					if (bx >= 0 && by >= 0 && bx < Main.maxTilesX && by < Main.maxTilesY)
					{
						Color c = Lighting.GetColor(bx, by);
						brightnessSum += (c.R + c.G + c.B) / (255f * 3f);
						samples++;
					}
				}
			}
			float brightness = samples > 0 ? brightnessSum / samples : 1f;

			// Other active players (for multiplayer proximity)
			var nearbyPlayers = new List<object>();
			for (int i = 0; i < Main.maxPlayers; i++)
			{
				var other = Main.player[i];
				if (other != null && other.active && i != Main.myPlayer)
				{
					nearbyPlayers.Add(new
					{
						name = other.name,
						x = other.position.X,
						y = other.position.Y,
						tileX = (int)(other.position.X / 16f),
						tileY = (int)(other.position.Y / 16f),
						hp = other.statLife,
						maxHp = other.statLifeMax2
					});
				}
			}

			var state = new
			{
				type = "state",
				tick = Main.GameUpdateCount,
				player = new
				{
					name = player.name,
					hp = player.statLife,
					maxHp = player.statLifeMax2,
					mana = player.statMana,
					maxMana = player.statManaMax2,
					x = player.position.X,
					y = player.position.Y,
					tileX = (int)(player.position.X / 16f),
					tileY = (int)(player.position.Y / 16f),
					velocityX = player.velocity.X,
					velocityY = player.velocity.Y,
					direction = player.direction,
					selectedItem = player.selectedItem,
					grounded = player.velocity.Y == 0f,
					brightness = MathF.Round(brightness, 2),
					breath = player.breath,
					breathMax = player.breathMax
				},
				hotbar,
				nearbyNpcs,
				nearbyPlayers,
				time = new
				{
					dayTime = Main.dayTime,
					time = Main.time,
					raining = Main.raining,
					bloodMoon = Main.bloodMoon
				}
			};

			Send(state);
		}

		private void PushNearbyTiles(int radius)
		{
			var player = Main.LocalPlayer;
			int cx = (int)(player.position.X / 16f);
			int cy = (int)(player.position.Y / 16f);

			var tiles = new List<object>();
			for (int x = cx - radius; x <= cx + radius; x++)
			{
				for (int y = cy - radius; y <= cy + radius; y++)
				{
					if (x < 0 || y < 0 || x >= Main.maxTilesX || y >= Main.maxTilesY)
						continue;
					var tile = Main.tile[x, y];
					if (tile.HasTile || tile.WallType > 0 || tile.LiquidAmount > 0)
					{
						tiles.Add(new
						{
							x,
							y,
							tileType = tile.HasTile ? tile.TileType : -1,
							wallType = tile.WallType,
							liquid = tile.LiquidAmount,
							liquidType = tile.LiquidType
						});
					}
				}
			}

			Send(new { type = "nearby_tiles", centerX = cx, centerY = cy, radius, tiles });
		}

		/// <summary>
		/// Called from GlobalNPC/ModPlayer to push combat events through the TCP bridge.
		/// </summary>
		public static void PushCombatEvent(string eventName, object data)
		{
			_instance?.SendEvent(eventName, data);
		}

		private void SendEvent(string eventName, object data)
		{
			Send(new { type = "event", @event = eventName, data });
		}

		private void Send(object obj)
		{
			lock (_lock)
			{
				if (_stream == null || !_client.Connected) return;
				try
				{
					string json = JsonSerializer.Serialize(obj);
					byte[] bytes = Encoding.UTF8.GetBytes(json + "\n");
					_stream.Write(bytes, 0, bytes.Length);
					_stream.Flush();
				}
				catch { }
			}
		}

		private static void ResetMovement()
		{
			ControlLeft = false;
			ControlRight = false;
			ControlUp = false;
			ControlDown = false;
			ControlJump = false;
		}

		private static void ResetControls()
		{
			ResetMovement();
			ControlUseItem = false;
			ControlQuickHeal = false;
			AutoMode = false;
			TargetTileX = -1;
			TargetTileY = -1;
		}
	}
}
