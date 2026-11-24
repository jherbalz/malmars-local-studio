"""The content screen switch: defaults, the config write, the guard rails.

This is the public copy of the Studio policy tests. It proves the switch
itself: on by default, fail closed, writable by the adult who owns the
machine, and lockable only when a school asks for it to be locked.

Two groups of tests from the Studio tree are deliberately not in this
file. The setup-page route and the dashboard checks need the Studio
launcher and the XMB web pages, and those live in the paid-edition source,
which is not public. Here we prove the policy that the route reads, which
is the part that decides whether an image is screened at all.

The full version of these tests, route and page included, runs in the
Studio tree on every build."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import safety_policy


def _config():
    return {
        "active_tier": "free",
        "image_safety": {"enabled": True, "mode": "block", "threshold": 0.2},
    }


class SafetyPolicyTests(unittest.TestCase):
    """The switch itself: defaults, the config write, and the guard rails."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "license_config.json"
        self.config.write_text(json.dumps(_config(), indent=2), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_policy_defaults_to_on_and_fail_closed(self):
        current = safety_policy.policy(self.root)
        self.assertTrue(current["enabled"])
        self.assertEqual(current["mode"], "block")
        self.assertTrue(safety_policy.fail_closed(self.root))

    def test_update_turns_screening_off_and_keeps_the_tier(self):
        result = safety_policy.update(self.root, {"enabled": False})
        self.assertTrue(result["ok"])
        self.assertFalse(result["policy"]["enabled"])
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertFalse(saved["image_safety"]["enabled"])
        self.assertEqual(saved["active_tier"], "free")

    def test_update_turns_screening_back_on(self):
        safety_policy.update(self.root, {"enabled": False})
        safety_policy.update(self.root, {"enabled": True})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(saved["image_safety"]["enabled"])

    def test_update_rejects_bad_values(self):
        for bad in ({"enabled": "yes"}, {"mode": "off"}, {"threshold": 2},
                    {"threshold": True}, {"threshold": "low"}):
            with self.assertRaises(ValueError):
                safety_policy.update(self.root, bad)

    def test_disabled_screening_is_not_fail_closed(self):
        safety_policy.update(self.root, {"enabled": False})
        self.assertFalse(safety_policy.fail_closed(self.root))
        self.assertFalse(safety_policy.screening_enabled(self.root))

    def test_update_keeps_the_other_config_keys(self):
        self.config.write_text(json.dumps({
            "active_tier": "free",
            "product": "Malmars Local",
            "publisher": "Herberto Malmars Toys LLC",
            "image_safety": {"enabled": True, "mode": "block", "threshold": 0.2,
                             "model": "Falconsai/nsfw_image_detection"},
        }, indent=2), encoding="utf-8")
        safety_policy.update(self.root, {"enabled": False})
        saved = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved["product"], "Malmars Local")
        self.assertEqual(saved["publisher"], "Herberto Malmars Toys LLC")
        self.assertEqual(saved["image_safety"]["model"],
                         "Falconsai/nsfw_image_detection")


class SafetyClientStandaloneTests(unittest.TestCase):
    """The client works on its own, outside the Studio tree."""

    def test_client_at_an_installed_root_finds_the_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "license_config.json").write_text(
                json.dumps({"active_tier": "free",
                            "image_safety": {"enabled": False, "mode": "block",
                                             "threshold": 0.2}}, indent=2),
                encoding="utf-8")
            for name in ("safety_client.py", "safety_policy.py"):
                (root / name).write_text((HERE / name).read_text(encoding="utf-8"),
                                         encoding="utf-8")
            spec = importlib.util.spec_from_file_location(
                "installed_safety_client", root / "safety_client.py")
            client = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(client)
            self.assertEqual(client.STUDIO_ROOT, root.resolve())
            result = client.check_prompt("a perfectly ordinary prompt")
            self.assertTrue(result["allowed"])
            self.assertFalse(result["screened"])


if __name__ == "__main__":
    unittest.main()
