from utils.encoding import safe_encode_string


def test_safe_encode_string_keeps_valid_text() -> None:
    assert safe_encode_string("hello") == "hello"


def test_safe_encode_string_handles_surrogates() -> None:
    # Lone surrogate cannot be encoded directly and should be normalized.
    broken = "bad\ud800text"
    normalized = safe_encode_string(broken)
    assert isinstance(normalized, str)
    assert "bad" in normalized


