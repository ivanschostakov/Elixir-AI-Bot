import unittest

from src.bot.order_codes import normalize_order_code_input


class NormalizeOrderCodeInputTests(unittest.TestCase):
    def test_accepts_supported_order_labels(self):
        examples = (
            "08-9VJ90",
            "Заказ №08-9VJ90",
            "# 08-9vj90",
            "Nº 08-9VJ90 от 10.08.2026",
            "08-9VJ90/2 от 10.08.2026",
        )
        for value in examples:
            with self.subTest(value=value):
                self.assertEqual(normalize_order_code_input(value), "08-9VJ90")

    def test_accepts_legacy_numeric_order_code(self):
        self.assertEqual(normalize_order_code_input("Заказ №1611232"), "1611232")

    def test_rejects_conversation_and_ambiguous_input(self):
        self.assertIsNone(normalize_order_code_input("Как применять селанк"))
        self.assertIsNone(normalize_order_code_input("08-9VJ90 или 10-7RX4O"))
        self.assertIsNone(normalize_order_code_input(None))


if __name__ == "__main__":
    unittest.main()
