import pytest

from app.core.gstin import STATE_CODES, checksum_char, validate_gstin, with_checksum
from app.seed.catalogs import ALL_BUSINESSES

# Widely published sample GSTINs (format examples). Two independent samples agreeing with the
# algorithm is the
# evidence for the checksum implementation; they are NOT claims about real registrations.
PUBLISHED_SAMPLES = ["27AAPFU0939F1ZV", "29AABCT1332L1ZA"]


@pytest.mark.parametrize("gstin", PUBLISHED_SAMPLES)
def test_published_samples_are_format_valid(gstin):
    result = validate_gstin(gstin)
    assert result.ok and result.normalized == gstin
    assert result.state_name == STATE_CODES[gstin[:2]]
    assert result.pan == gstin[2:12]


def test_lowercase_and_whitespace_are_normalised():
    assert validate_gstin("  27aapfu0939f1zv ").normalized == "27AAPFU0939F1ZV"


@pytest.mark.parametrize(
    "value,error",
    [
        ("27AAPFU0939F1ZX", "bad_checksum"),
        ("27AAPFU0939F1Z", "bad_length"),
        ("", "bad_length"),
        ("27AAPFU0939F1ZVV", "bad_length"),
        ("27AAPFU0939F1AV", "bad_format"),  # 14th character must be 'Z'
        ("2AAAPFU0939F1ZV", "bad_format"),
        ("00AAPFU0939F1ZV", "unknown_state"),
    ],
)
def test_invalid_values_report_a_machine_readable_reason(value, error):
    result = validate_gstin(value)
    assert not result.ok and result.error == error


def test_with_checksum_round_trips():
    assert with_checksum("27AAPFU0939F1Z") == "27AAPFU0939F1ZV"
    assert checksum_char("27AAPFU0939F1Z") == "V"


def test_seed_gstins_are_format_valid_and_state_consistent():
    registered = [b for b in ALL_BUSINESSES if b.gstin]
    assert len(registered) == 3
    for business in registered:
        result = validate_gstin(business.gstin)
        assert result.ok, business.key
        assert result.state_code == business.state_code
