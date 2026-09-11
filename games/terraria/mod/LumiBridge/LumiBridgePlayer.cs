using Terraria;
using Terraria.DataStructures;
using Terraria.GameInput;
using Terraria.ModLoader;

namespace LumiBridge
{
	/// <summary>
	/// Injects control flags into the local player's input every frame.
	/// Also tracks combat events (damage taken, death).
	///
	/// Key insight: Player.Update() execution order is:
	///   PreUpdate → ResetControls() → if(hasFocus){CopyInto → SetControls} → ...buffs/equips...
	///   → PostUpdateRunSpeeds → HorizontalMovement() → ...jump... → PreUpdateMovement → position update → PostUpdate
	///
	/// When window loses focus, ResetControls clears all flags and the hasFocus block is skipped.
	/// We inject in PostUpdateRunSpeeds which fires AFTER the reset, right before movement code.
	/// </summary>
	public class LumiBridgePlayer : ModPlayer
	{
		/// <summary>
		/// PostUpdateRunSpeeds fires after ResetControls + hasFocus check, right before HorizontalMovement().
		/// This is the correct injection point that works regardless of window focus.
		/// </summary>
		public override void PostUpdateRunSpeeds()
		{
			if (!LumiBridgeSystem.AutoMode) return;
			if (Player.whoAmI != Main.myPlayer) return;

			// Inject movement controls
			if (LumiBridgeSystem.ControlLeft)
				Player.controlLeft = true;
			if (LumiBridgeSystem.ControlRight)
				Player.controlRight = true;
			if (LumiBridgeSystem.ControlUp)
				Player.controlUp = true;
			if (LumiBridgeSystem.ControlDown)
				Player.controlDown = true;
			if (LumiBridgeSystem.ControlJump)
				Player.controlJump = true;

			// Quick heal (H key equivalent — one-shot, auto-resets)
			if (LumiBridgeSystem.ControlQuickHeal)
			{
				Player.controlQuickHeal = true;
				LumiBridgeSystem.ControlQuickHeal = false;
			}

			// Inject item use (mining, placing, attacking)
			if (LumiBridgeSystem.ControlUseItem)
			{
				Player.controlUseItem = true;

				// Point cursor at target tile for mining/placing
				if (LumiBridgeSystem.TargetTileX >= 0 && LumiBridgeSystem.TargetTileY >= 0)
				{
					int tx = LumiBridgeSystem.TargetTileX;
					int ty = LumiBridgeSystem.TargetTileY;

					// Set mouse position (for swing direction)
					float worldX = tx * 16f + 8f;
					float worldY = ty * 16f + 8f;
					Main.mouseX = (int)(worldX - Main.screenPosition.X);
					Main.mouseY = (int)(worldY - Main.screenPosition.Y);
				}
			}

			// hold_use_item: hold use button for N frames then auto-release
			if (LumiBridgeSystem.HoldUseFrames > 0)
			{
				Player.controlUseItem = true;
				LumiBridgeSystem.HoldUseFrames--;
			}
		}

		/// <summary>
		/// Runs right before item usage logic — AFTER the game calculates tileTargetX/Y
		/// from mouse position and smart cursor. We override it here so our target sticks.
		/// </summary>
		public override bool PreItemCheck()
		{
			if (!LumiBridgeSystem.AutoMode) return true;
			if (Player.whoAmI != Main.myPlayer) return true;

			if (LumiBridgeSystem.ControlUseItem
				&& LumiBridgeSystem.TargetTileX >= 0
				&& LumiBridgeSystem.TargetTileY >= 0)
			{
				Player.tileTargetX = LumiBridgeSystem.TargetTileX;
				Player.tileTargetY = LumiBridgeSystem.TargetTileY;
			}

			return true;
		}

		public override void OnHurt(Player.HurtInfo info)
		{
			if (Player.whoAmI != Main.myPlayer) return;

			// 追溯伤害来源名称：NPC直接碰撞 → 投射物(追溯发射者NPC) → 其他玩家 → 未知
			string sourceName = "未知";
			var ds = info.DamageSource;
			if (ds.SourceNPCIndex >= 0)
			{
				sourceName = Main.npc[ds.SourceNPCIndex].FullName;
			}
			else if (ds.SourceProjectileLocalIndex >= 0)
			{
				var proj = Main.projectile[ds.SourceProjectileLocalIndex];
				// 敌对投射物：追溯发射它的NPC
				if (proj.npcProj || proj.hostile)
				{
					// 某些投射物记录了 owner 为 NPC index (通过 Projectile.ai[] 或直接字段)
					// 但 Terraria 对敌方弹幕没有统一的 "ownerNPC" 字段
					// 最可靠的方式是用投射物自身的名字
					string projName = proj.Name;
					if (!string.IsNullOrEmpty(projName) && projName != "")
						sourceName = projName;
					else
						sourceName = "敌方投射物";
				}
				else if (ds.SourcePlayerIndex >= 0)
				{
					sourceName = Main.player[ds.SourcePlayerIndex].name;
				}
			}
			else if (ds.SourcePlayerIndex >= 0)
			{
				sourceName = Main.player[ds.SourcePlayerIndex].name;
			}
			else if (ds.SourceOtherIndex >= 0)
			{
				// SourceOther: 0=fall, 1=drown, 2=lava, 3=debuff, etc.
				switch (ds.SourceOtherIndex)
				{
					case 0: sourceName = "摔落"; break;
					case 1: sourceName = "溺水"; break;
					case 2: sourceName = "岩浆"; break;
					default: sourceName = "环境伤害"; break;
				}
			}

			LumiBridgeSystem.PushCombatEvent("player_hurt", new
			{
				damage = info.Damage,
				hp = Player.statLife,
				maxHp = Player.statLifeMax2,
				source = sourceName
			});
		}

		public override void Kill(double damage, int hitDirection, bool pvp, PlayerDeathReason damageSource)
		{
			if (Player.whoAmI != Main.myPlayer) return;

			LumiBridgeSystem.PushCombatEvent("player_died", new
			{
				deathMessage = damageSource.GetDeathText(Player.name).ToString()
			});
		}
	}
}
