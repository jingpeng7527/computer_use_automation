"""_typed_value -- no browser, no LLM.

Regression coverage for a real precision bug: money was parsed via
`float(...) * 100`, which cannot represent every two-decimal dollar amount
exactly. At large magnitudes that showed up as real cents lost --
"$99999999999999.99" rounded to 9999999999999998 minor units instead of
the correct 9999999999999999 -- directly contradicting REPORT.md's
"money is never a float" claim about the arithmetic, not just the final
type.
"""

from __future__ import annotations

from cua.replay.engine import _typed_value


def test_money_parses_via_decimal_with_no_precision_loss_at_large_amounts() -> None:
    value = _typed_value("$99999999999999.99 USD", "money")
    assert value.amount_minor == 9999999999999999
    assert value.currency == "USD"


def test_money_parses_an_ordinary_amount_correctly() -> None:
    value = _typed_value("$8160.00 USD", "money")
    assert value.amount_minor == 816000
    assert value.currency == "USD"


def test_money_falls_back_to_the_raw_string_when_unparseable() -> None:
    assert _typed_value("not a dollar amount", "money") == "not a dollar amount"


def test_integer_and_boolean_still_convert() -> None:
    assert _typed_value("42", "integer") == 42
    assert _typed_value("true", "boolean") is True
    assert _typed_value("no", "boolean") is False
