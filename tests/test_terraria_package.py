from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MOD_DIR = ROOT / "games" / "terraria" / "mod" / "LumiBridge"


class TerrariaPackageTests(unittest.TestCase):
    def test_required_mod_sources_are_published(self):
        required = {
            "build.txt",
            "description.txt",
            "LumiBridge.cs",
            "LumiBridgeGlobalNPC.cs",
            "LumiBridgePlayer.cs",
            "LumiBridgeSystem.cs",
        }
        published = {path.name for path in MOD_DIR.iterdir() if path.is_file()}
        self.assertTrue(required.issubset(published))

    def test_background_control_hook_runs_after_focus_reset(self):
        player_source = (MOD_DIR / "LumiBridgePlayer.cs").read_text(encoding="utf-8-sig")
        self.assertIn("public override void PostUpdateRunSpeeds()", player_source)
        self.assertNotIn("public override void SetControls()", player_source)
        self.assertIn("Player.controlLeft = true", player_source)
        self.assertIn("Player.controlRight = true", player_source)

    def test_inactive_client_keeps_updating(self):
        system_source = (MOD_DIR / "LumiBridgeSystem.cs").read_text(encoding="utf-8-sig")
        self.assertIn('MOD_VERSION = "5.3"', system_source)
        self.assertIn("InactiveSleepTime = TimeSpan.Zero", system_source)
        self.assertIn("Main.autoPause = false", system_source)

    def test_bot_builds_bundled_source_and_checks_protocol_version(self):
        bot_source = (ROOT / "games" / "terraria" / "bot.py").read_text(encoding="utf-8")
        self.assertIn('Path(__file__).resolve().parent / "mod" / "LumiBridge"', bot_source)
        self.assertIn('EXPECTED_MOD_VERSION = "5.3"', bot_source)
        self.assertIn("已停止自动控制", bot_source)


if __name__ == "__main__":
    unittest.main()
