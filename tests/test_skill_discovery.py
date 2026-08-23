import json
import tempfile
import unittest
from pathlib import Path

from lovstudio_skill_helper import config


class SkillDiscoveryTests(unittest.TestCase):
    def test_current_canonical_runtime_directory_is_first(self):
        home = Path("/tmp/lovstudio-helper-home")
        candidates = config.skill_dir_candidates("write-professional-book", home)
        self.assertEqual(
            candidates[0],
            home / ".agents" / "skills" / "lov-write-professional-book",
        )
        self.assertIn(
            home / ".claude" / "skills" / "lovstudio-write-professional-book",
            candidates,
        )

    def test_finds_current_installer_bundle_from_product_or_runtime_name(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            skill_dir = home / ".agents" / "skills" / "lov-write-professional-book"
            skill_dir.mkdir(parents=True)
            (skill_dir / "MANIFEST.enc.json").write_text(
                json.dumps({"skill_name": "write-professional-book"})
            )

            self.assertEqual(
                config.skill_dir("write-professional-book", home),
                skill_dir,
            )
            self.assertEqual(
                config.skill_dir("lov-write-professional-book", home),
                skill_dir,
            )
            self.assertEqual(
                config.installed_skills(home),
                ["write-professional-book"],
            )


if __name__ == "__main__":
    unittest.main()
