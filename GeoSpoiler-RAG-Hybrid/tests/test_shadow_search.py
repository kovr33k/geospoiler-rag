import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval import shadow_search  # noqa: E402


class ShadowSearchTokenTests(unittest.TestCase):
    def test_matches_short_slavic_inflection_variants_by_stem(self):
        self.assertTrue(shadow_search._matches_term("нарвы", "нарву"))
        self.assertTrue(shadow_search._matches_term("эстонии", "эстонию"))

    def test_does_not_match_unrelated_long_prefix_words(self):
        self.assertFalse(shadow_search._matches_term("протесты", "против"))


    def test_does_not_match_ultraleft_to_ultraright_shared_prefix(self):
        self.assertFalse(shadow_search._matches_term("ультраправых", "ультралевых"))
        self.assertFalse(shadow_search._matches_term("ультралевых", "ультраправых"))
        self.assertFalse(shadow_search._matches_term("ультра", "ультралевых"))

    def test_does_not_match_odnak_expansion_to_plain_one(self):
        self.assertFalse(shadow_search._matches_term("один", "одинак"))

    def test_query_terms_expand_afd_alias(self):
        terms = shadow_search.query_terms("AfD Ukraine")

        self.assertIn("afd", terms)
        self.assertIn("адг", terms)


if __name__ == "__main__":
    unittest.main()
