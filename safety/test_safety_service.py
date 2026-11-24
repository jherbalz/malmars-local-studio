import unittest
from unittest.mock import patch

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import safety_service


class PromptPolicyTests(unittest.TestCase):
    def test_allows_ordinary_asset_prompt(self):
        self.assertTrue(safety_service.check_prompt(
            "A friendly cartoon robot standing in a garden."
        )["allowed"])

    def test_blocks_explicit_and_graphic_requests(self):
        for text in ("make erotic art", "show graphic violence", "self-harm scene"):
            with self.subTest(text=text), self.assertRaises(safety_service.SafetyError):
                safety_service.check_prompt(text)

    def test_invalid_prompt_fails_closed(self):
        with self.assertRaises(safety_service.SafetyError):
            safety_service.check_prompt(None)


class ImagePolicyTests(unittest.TestCase):
    def test_rejects_unapproved_mime_before_model_load(self):
        with self.assertRaises(safety_service.SafetyError) as caught:
            safety_service.check_image(b"image", "image/gif")
        self.assertEqual(caught.exception.status, 415)

    def test_model_missing_fails_closed(self):
        from PIL import Image
        from io import BytesIO

        output = BytesIO()
        Image.new("RGB", (1, 1), "white").save(output, format="PNG")
        with patch.object(safety_service, "model_status",
                          return_value={"installed": False}):
            with self.assertRaises(safety_service.SafetyError) as caught:
                safety_service.check_image(output.getvalue(), "image/png")
        self.assertEqual(caught.exception.status, 503)


if __name__ == "__main__":
    unittest.main()
