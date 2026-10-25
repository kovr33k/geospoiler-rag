import unittest
from unittest.mock import patch

import config
from normalizer.media_vision import _current_api_keys


class CurrentVisualFallbackTests(unittest.TestCase):
    def test_fallback_key_requires_explicit_policy(self):
        with patch.object(config, "VISION_API_KEY", "vision"), patch.object(
            config, "LLM_API_KEY", "llm"
        ), patch.object(config, "MEDIA_VISION_FALLBACK_ENABLED", False):
            self.assertEqual(_current_api_keys(), ["vision"])
        with patch.object(config, "VISION_API_KEY", "vision"), patch.object(
            config, "LLM_API_KEY", "llm"
        ), patch.object(config, "MEDIA_VISION_FALLBACK_ENABLED", True):
            self.assertEqual(_current_api_keys(), ["vision", "llm"])


if __name__ == "__main__":
    unittest.main()
