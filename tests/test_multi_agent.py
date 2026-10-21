import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from minecraft_harness.config import validate_config

ROOT = Path(__file__).resolve().parents[1]


class MultiAgentTests(unittest.TestCase):
    def test_config_accepts_one_to_eight_agents_longrun(self):
        for count in (1, 3, 5, 6, 8, 16):
            with self.subTest(count=count):
                cfg = validate_config({"agents": {"count": count}})
                self.assertEqual(cfg["agents"]["count"], count)
        for count in (0, 17):
            with self.subTest(count=count):
                with self.assertRaises(ValueError):
                    validate_config({"agents": {"count": count}})

    def test_paper_profile_still_requires_four(self):
        with self.assertRaises(ValueError):
            validate_config({"experiment": {"profile": "paper_aligned"}, "agents": {"count": 6}})

    def test_render_compose_exact_agent_count(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "compose.yaml"
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "render_compose.py"),
                 "--agents", "6", "--mode", "strict", "--output", str(output)],
                check=True, capture_output=True, text=True)
            rendered = yaml.safe_load(output.read_text())
        self.assertEqual(rendered["name"], "minecraft-paper")
        self.assertEqual(sorted(s for s in rendered["services"] if s.startswith("bot")),
                         [f"bot{i}" for i in range(1, 7)])
        self.assertEqual(sorted(s for s in rendered["services"] if s.startswith("identity")),
                         [f"identity{i}" for i in range(1, 7)])
        self.assertEqual(sorted(s for s in rendered["services"] if s.startswith("launcher")),
                         [f"launcher{i}" for i in range(1, 7)])
        self.assertIn("server", rendered["services"])
        self.assertEqual(sorted(n for n in rendered["networks"] if n.startswith("worker")),
                         [f"worker{i}" for i in range(1, 7)])
        for index in range(1, 7):
            env = rendered["services"][f"bot{index}"]["environment"]
            self.assertEqual(env["ALLOWED_AGENT_ID"], f"player{index}")
            self.assertEqual(env["LIFECYCLE"], "strict")
            self.assertIn(f"BOT{index}_ADMIN_TOKEN", env["TOKEN"])
            self.assertIn(f"WORKER{index}_TOKEN", env["WORKER_ENDPOINTS_JSON"])
            self.assertIn(f"tcp://launcher{index}:390{index}", env["WORKER_ENDPOINTS_JSON"])


if __name__ == "__main__":
    unittest.main()
