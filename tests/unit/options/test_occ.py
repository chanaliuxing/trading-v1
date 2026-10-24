"""Unit tests for OCC symbol utilities."""

import pytest
from libs.options.occ import (
    build_occ_symbol,
    parse_occ_symbol,
    validate_occ_symbol,
    get_contract_description,
)


class TestBuildOCCSymbol:
    """Test OCC symbol building."""

    def test_build_spy_call(self):
        """Test building SPY call option."""
        symbol = build_occ_symbol("SPY", "20250120", "C", 450.0)
        assert symbol == "SPY   250120C00450000"

    def test_build_qqq_put(self):
        """Test building QQQ put option."""
        symbol = build_occ_symbol("QQQ", "2025-01-20", "P", 380.5)
        assert symbol == "QQQ   250120P00380500"

    def test_build_with_yymmdd(self):
        """Test building with 6-digit date."""
        symbol = build_occ_symbol("SPY", "250120", "C", 450.0)
        assert symbol == "SPY   250120C00450000"

    def test_build_fractional_strike(self):
        """Test building with fractional strike."""
        symbol = build_occ_symbol("SPY", "20250120", "P", 449.75)
        assert symbol == "SPY   250120P00449750"

    def test_build_lowercase_inputs(self):
        """Test that lowercase inputs are normalized."""
        symbol = build_occ_symbol("spy", "20250120", "c", 450.0)
        assert symbol == "SPY   250120C00450000"

    def test_build_short_symbol(self):
        """Test with short underlying symbol."""
        symbol = build_occ_symbol("F", "20250120", "C", 12.5)
        assert symbol == "F     250120C00012500"

    def test_invalid_underlying_too_long(self):
        """Test rejection of too-long underlying."""
        with pytest.raises(ValueError, match="must be 1-6 characters"):
            build_occ_symbol("TOOLONG", "20250120", "C", 450.0)

    def test_invalid_date_format(self):
        """Test rejection of invalid date format."""
        with pytest.raises(ValueError, match="Invalid expiry date"):
            build_occ_symbol("SPY", "2025-13-45", "C", 450.0)

    def test_invalid_right(self):
        """Test rejection of invalid right."""
        with pytest.raises(ValueError, match="must be 'C' or 'P'"):
            build_occ_symbol("SPY", "20250120", "X", 450.0)

    def test_invalid_strike_negative(self):
        """Test rejection of negative strike."""
        with pytest.raises(ValueError, match="must be positive"):
            build_occ_symbol("SPY", "20250120", "C", -10.0)

    def test_invalid_strike_zero(self):
        """Test rejection of zero strike."""
        with pytest.raises(ValueError, match="must be positive"):
            build_occ_symbol("SPY", "20250120", "C", 0.0)

    def test_strike_too_large(self):
        """Test rejection of strike too large for format."""
        with pytest.raises(ValueError, match="too large"):
            build_occ_symbol("SPY", "20250120", "C", 100000.0)


class TestParseOCCSymbol:
    """Test OCC symbol parsing."""

    def test_parse_spy_call(self):
        """Test parsing SPY call."""
        parsed = parse_occ_symbol("SPY   250120C00450000")
        assert parsed == {
            "underlying": "SPY",
            "expiry_date": "2025-01-20",
            "right": "C",
            "strike": 450.0,
        }

    def test_parse_qqq_put(self):
        """Test parsing QQQ put."""
        parsed = parse_occ_symbol("QQQ   250120P00380500")
        assert parsed == {
            "underlying": "QQQ",
            "expiry_date": "2025-01-20",
            "right": "P",
            "strike": 380.5,
        }

    def test_parse_fractional_strike(self):
        """Test parsing fractional strike."""
        parsed = parse_occ_symbol("SPY   250120P00449750")
        assert parsed["strike"] == 449.75

    def test_parse_single_char_symbol(self):
        """Test parsing single-character underlying."""
        parsed = parse_occ_symbol("F     250120C00012500")
        assert parsed["underlying"] == "F"
        assert parsed["strike"] == 12.5

    def test_roundtrip(self):
        """Test build -> parse roundtrip."""
        original = {"underlying": "SPY", "expiry": "2025-01-20", "right": "C", "strike": 450.0}
        built = build_occ_symbol(original["underlying"], original["expiry"], original["right"], original["strike"])
        parsed = parse_occ_symbol(built)

        assert parsed["underlying"] == original["underlying"]
        assert parsed["expiry_date"] == original["expiry"]
        assert parsed["right"] == original["right"]
        assert parsed["strike"] == original["strike"]

    def test_invalid_length(self):
        """Test rejection of wrong length symbol."""
        with pytest.raises(ValueError, match="must be 21 characters"):
            parse_occ_symbol("SPY250120C00450000")  # Too short

    def test_invalid_underlying(self):
        """Test rejection of invalid underlying."""
        with pytest.raises(ValueError, match="Invalid underlying"):
            parse_occ_symbol("12345 250120C00450000")

    def test_invalid_expiry_format(self):
        """Test rejection of invalid expiry."""
        with pytest.raises(ValueError, match="Invalid expiry format"):
            parse_occ_symbol("SPY   25X120C00450000")

    def test_invalid_right(self):
        """Test rejection of invalid right."""
        with pytest.raises(ValueError, match="Invalid right"):
            parse_occ_symbol("SPY   250120X00450000")

    def test_invalid_strike_format(self):
        """Test rejection of invalid strike format."""
        with pytest.raises(ValueError, match="Invalid strike format"):
            parse_occ_symbol("SPY   250120C0045X000")


class TestValidateOCCSymbol:
    """Test OCC symbol validation."""

    def test_valid_symbols(self):
        """Test validation of valid symbols."""
        assert validate_occ_symbol("SPY   250120C00450000") is True
        assert validate_occ_symbol("QQQ   250120P00380500") is True
        assert validate_occ_symbol("F     250120C00012500") is True

    def test_invalid_symbols(self):
        """Test validation of invalid symbols."""
        assert validate_occ_symbol("INVALID") is False
        assert validate_occ_symbol("SPY250120C00450000") is False  # Too short
        assert validate_occ_symbol("12345 250120C00450000") is False  # Bad underlying
        assert validate_occ_symbol("SPY   250120X00450000") is False  # Bad right


class TestGetContractDescription:
    """Test human-readable contract descriptions."""

    def test_spy_call_description(self):
        """Test SPY call description."""
        desc = get_contract_description("SPY   250120C00450000")
        assert desc == "SPY 2025-01-20 450.00 Call"

    def test_qqq_put_description(self):
        """Test QQQ put description."""
        desc = get_contract_description("QQQ   250120P00380500")
        assert desc == "QQQ 2025-01-20 380.50 Put"

    def test_invalid_symbol_description(self):
        """Test description of invalid symbol."""
        desc = get_contract_description("INVALID")
        assert "Invalid" in desc


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
