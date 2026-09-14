"""Redaction -- no browser, no LLM. The fourth of 系统设计.md sec 1.7's four
test categories. Values are masked, not hashed or omitted: a log that
can't distinguish two runs of the same capability isn't debuggable, and
evidence has to explain a run (REPORT.md sec 6) -- so the shape asserted
throughout is `<tag:N digits|chars>`, never a bare removal.
"""

from __future__ import annotations

from pathlib import Path

from cua.safety import load_policy, mask, redact_ctx_value, redact_text, redact_value

POLICY = load_policy(Path(__file__).parent.parent / "config" / "policy.yaml")


def test_mask_reports_digit_count_for_numeric_values() -> None:
    assert mask("12345", "pii") == "<pii:5 digits>"


def test_mask_reports_char_count_for_non_numeric_values() -> None:
    assert mask("Dolores Ibarra", "pii") == "<pii:14 chars>"


def test_declared_sensitivity_masks_regardless_of_content() -> None:
    # "hello" matches none of the regex patterns, but sensitivity says pii.
    assert redact_value("hello", sensitivity="pii", policy=POLICY) == "<pii:5 chars>"


def test_sensitive_field_name_masks_even_without_declared_sensitivity() -> None:
    value = redact_value("hunter2", sensitivity="none", field_name="password", policy=POLICY)
    assert value == "<sensitive:7 chars>"


def test_regex_fallback_catches_an_undeclared_ssn_shape() -> None:
    value = redact_value("123-45-6789", sensitivity="none", policy=POLICY)
    assert value.startswith("<ssn:")


def test_non_sensitive_value_passes_through_unchanged() -> None:
    assert redact_value("$8160.00 USD", sensitivity="none", policy=POLICY) == "$8160.00 USD"


def test_ctx_value_is_always_masked_regardless_of_sensitivity() -> None:
    # No sensitivity argument at all -- ctx values are masked
    # unconditionally, per 系统设计 sec 2.4.
    assert redact_ctx_value("SECRET-TOKEN-123") == "<ctx:16 chars>"


def test_redact_text_masks_a_pattern_found_inside_free_text() -> None:
    text = "lookup failed for account 123456789 during processing"
    redacted = redact_text(text, POLICY)
    assert "123456789" not in redacted
    assert "<long_number>" in redacted


def test_redact_text_leaves_ordinary_text_untouched() -> None:
    text = "reached a page headed 'Member Detail'"
    assert redact_text(text, POLICY) == text
