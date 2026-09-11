using Terraria;
using Terraria.ModLoader;

namespace LumiBridge
{
	/// <summary>
	/// Tracks NPC kills near the player and pushes combat events.
	/// </summary>
	public class LumiBridgeGlobalNPC : GlobalNPC
	{
		public override void OnKill(NPC npc)
		{
			var player = Main.LocalPlayer;
			if (player == null || !player.active) return;

			// Only report kills within a reasonable range (1200 pixels ~ 75 tiles)
			if (npc.Distance(player.Center) > 1200f) return;

			LumiBridgeSystem.PushCombatEvent("npc_killed", new
			{
				name = npc.FullName,
				id = npc.type,
				lifeMax = npc.lifeMax,
				x = npc.position.X,
				y = npc.position.Y,
				friendly = npc.friendly,
				boss = npc.boss,
				value = (int)npc.value // coin drop value
			});
		}
	}
}
