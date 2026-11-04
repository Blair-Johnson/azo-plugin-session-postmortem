"""Delivery accounting used by the saved-workspace inspection regression check."""
import unittest

from scripts.check_inspection import anchor_key, delivered_costs


class ReplayCheckerTests(unittest.TestCase):
    def test_intact_card_and_mixed_source_costs(self):
        card = {"entry_id": "e", "card": {"id": "e", "note": "ñ"}}
        expected = len('{"id":"e","note":"ñ"}')
        self.assertEqual(delivered_costs([card]), {("e", "card"): expected})
        text = {"anchor": {"event": "S/e1", "field": "text", "lines": [1, 1]}, "text": "abc"}
        self.assertEqual(delivered_costs([card, text]), {("e", "card"): expected, ("S/e1", "text"): 3})

    def test_unavailable_bare_refs_and_split_cards(self):
        missing = {"anchor": "S/o-missing", "available": False}
        self.assertEqual(anchor_key(missing["anchor"]), ("S/o-missing", "text"))
        self.assertEqual(delivered_costs([missing]), {})
        first = {"anchor": {"event": "S/e1", "field": "text"}, "text": "abc"}
        self.assertEqual(delivered_costs([first, missing]), {("S/e1", "text"): 3})
        split = {"entry_id": "e", "format": "map_card_json", "text": '{"i', "char_offset": 0}
        self.assertEqual(delivered_costs([split]), {("e", "card"): 3})


if __name__ == "__main__":
    unittest.main()
