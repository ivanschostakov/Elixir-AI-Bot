import unittest

from src.bot.broadcast_limits import broadcast_text_limit, telegram_text_length


class BroadcastLimitsTests(unittest.TestCase):
    def test_counts_visible_html_text(self):
        self.assertEqual(telegram_text_length("<b>Hello</b> &amp; <i>bye</i>"), 11)

    def test_counts_emoji_as_two_utf16_units(self):
        self.assertEqual(telegram_text_length("A😀B"), 4)

    def test_uses_caption_limit_for_photos(self):
        self.assertEqual(broadcast_text_limit(has_photos=False), 4096)
        self.assertEqual(broadcast_text_limit(has_photos=True), 1024)


if __name__ == "__main__":
    unittest.main()
