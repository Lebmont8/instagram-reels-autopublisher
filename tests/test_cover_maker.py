#!/usr/bin/env python3
import unittest

import cover_maker


class CoverSelectionTest(unittest.TestCase):
    def test_equal_scores_use_stable_scene_order(self) -> None:
        package = {
            "scenes": [
                {
                    "image_path": "/tmp/first.jpg",
                    "scene_function": "problem",
                    "intensity": 3,
                    "emotion_tags": [],
                },
                {
                    "image_path": "/tmp/second.jpg",
                    "scene_function": "problem",
                    "intensity": 3,
                    "emotion_tags": [],
                },
            ]
        }
        self.assertEqual("/tmp/first.jpg", cover_maker.choose_image(package, []))

    def test_cover_title_layout_stays_inside_instagram_safe_zone(self) -> None:
        layout = cover_maker.layout_title("Customers should not have to wait")
        self.assertLessEqual(len(layout["lines"]), cover_maker.MAX_TEXT_LINES)
        self.assertGreaterEqual(layout["x_min"], 120)
        self.assertLessEqual(layout["x_max"], cover_maker.WIDTH - 120)
        self.assertGreaterEqual(layout["y_min"], cover_maker.SAFE_TOP)
        self.assertLessEqual(layout["y_max"], cover_maker.HEIGHT - cover_maker.SAFE_BOTTOM)

    def test_long_cover_title_shrinks_and_wraps(self) -> None:
        layout = cover_maker.layout_title("Fast answers often matter more than discounts")
        self.assertLessEqual(len(layout["lines"]), cover_maker.MAX_TEXT_LINES)
        self.assertLessEqual(layout["font_size"], cover_maker.MAX_FONT_SIZE)

    def test_title_bank_is_specific_to_payment_problem(self) -> None:
        package = {"variant_id": "uk_payment_link", "scenes": []}
        title = cover_maker.choose_title(package, "payment-slug", [])
        self.assertIn(title, cover_maker.TITLE_BANK["payment_link"])


if __name__ == "__main__":
    unittest.main()
