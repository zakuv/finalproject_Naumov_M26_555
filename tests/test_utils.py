"""Проверки валидации и расчёта учебных обменных курсов."""

import unittest

from valutatrade_hub.core.utils import (
    get_exchange_rate,
    validate_currency_code,
    validate_number,
    validate_password,
    validate_username,
)


class ValidationTests(unittest.TestCase):
    """Проверить нормализацию и отклонение недопустимых значений."""

    def test_username_is_trimmed_without_changing_internal_spaces(self):
        """Пробелы по краям убираются, содержимое имени сохраняется."""
        self.assertEqual(validate_username("  Alice Smith  "), "Alice Smith")
        for invalid in ("", "  ", None, 123, True):
            with self.subTest(username=invalid):
                with self.assertRaises(ValueError):
                    validate_username(invalid)

    def test_password_requires_four_characters_and_keeps_exact_text(self):
        """Пароль сохраняется без нормализации и имеет минимальную длину."""
        self.assertEqual(validate_password("abcd"), "abcd")
        self.assertEqual(validate_password("  pass word  "), "  pass word  ")
        for invalid in ("", "abc", None, 1234, True):
            with self.subTest(password=invalid):
                with self.assertRaises(ValueError):
                    validate_password(invalid)

    def test_currency_codes_use_two_to_five_ascii_letters(self):
        """Коды нормализуются, цифры, Unicode и неверная длина отклоняются."""
        for original, expected in ((" usd ", "USD"), ("ab", "AB"), ("abcde", "ABCDE")):
            with self.subTest(code=original):
                self.assertEqual(validate_currency_code(original), expected)
        for invalid in ("", "A", "ABCDEF", "US1", "EU-R", "РУБ", "éUR", None, 1):
            with self.subTest(code=invalid):
                with self.assertRaises(ValueError):
                    validate_currency_code(invalid)

    def test_number_accepts_finite_positive_ints_and_floats(self):
        """Обычные целые и дробные суммы проходят проверку."""
        self.assertEqual(validate_number(2), 2)
        self.assertEqual(validate_number(0.125), 0.125)
        self.assertEqual(validate_number(0, "balance", allow_zero=True), 0)

    def test_invalid_numbers_are_rejected_even_when_zero_is_allowed(self):
        """Строки, bool, отрицательные и неконечные числа недопустимы."""
        invalid_values = (
            -1,
            float("nan"),
            float("inf"),
            -float("inf"),
            True,
            "2",
            None,
        )
        for invalid in invalid_values:
            for allow_zero in (False, True):
                with self.subTest(value=invalid, allow_zero=allow_zero):
                    with self.assertRaises(ValueError):
                        validate_number(invalid, allow_zero=allow_zero)
        with self.assertRaises(ValueError):
            validate_number(0)


class ExchangeRateTests(unittest.TestCase):
    """Проверить направление курса и отсутствие скрытых ставок по умолчанию."""

    def test_direct_and_inverse_rates_have_correct_direction(self):
        """Обратный курс равен единице, делённой на прямой."""
        rates = {"EUR_USD": 2.0}
        self.assertEqual(get_exchange_rate(" eur ", "usd", rates), 2)
        self.assertAlmostEqual(get_exchange_rate("USD", "EUR", rates), 0.5)
        self.assertEqual(rates, {"EUR_USD": 2.0})

    def test_cross_rate_can_use_direct_or_inverse_usd_legs(self):
        """BTC→EUR вычисляется через общую базу USD в обоих направлениях."""
        rates = {"BTC_USD": 60000.0, "EUR_USD": 1.2}
        self.assertAlmostEqual(get_exchange_rate("BTC", "EUR", rates), 50000)
        inverse_rates = {"USD_BTC": 1 / 60000, "USD_EUR": 1 / 1.2}
        self.assertAlmostEqual(get_exchange_rate("BTC", "EUR", inverse_rates), 50000)

    def test_identical_currency_needs_no_quote(self):
        """Курс валюты к себе равен единице даже при пустой карте."""
        self.assertEqual(get_exchange_rate("usd", " USD ", {}), 1)

    def test_default_demo_quotes_match_the_stage_contract(self):
        """Без карты курсов используются только согласованные учебные значения."""
        expected = {
            "EUR": 1.0786,
            "BTC": 59337.21,
            "RUB": 0.01016,
            "ETH": 3720,
        }
        for code, rate in expected.items():
            with self.subTest(currency=code):
                self.assertAlmostEqual(get_exchange_rate(code, "USD"), rate)

    def test_missing_quote_does_not_fall_back_from_explicit_empty_mapping(self):
        """Переданная карта полностью определяет доступные курсы."""
        for rates in ({}, {"BTC_USD": 60000}):
            with self.subTest(rates=rates):
                with self.assertRaises(ValueError):
                    get_exchange_rate("EUR", "USD", rates)
        with self.assertRaises(ValueError):
            get_exchange_rate("SOL", "USD")

    def test_invalid_requested_quote_is_rejected(self):
        """Не допускаются нулевые, отрицательные и нечисловые курсы."""
        invalid_rates = (0, -1, float("nan"), float("inf"), True, "1.2")
        for rate in invalid_rates:
            for pair in ("EUR_USD", "USD_EUR"):
                with self.subTest(pair=pair, rate=rate):
                    with self.assertRaises(ValueError):
                        get_exchange_rate("EUR", "USD", {pair: rate})


if __name__ == "__main__":
    unittest.main()
