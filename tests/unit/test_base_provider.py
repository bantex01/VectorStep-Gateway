"""Tests for base provider helpers: raise_if_error_envelope, ProviderError."""
import pytest

from gateway.llm.providers.base import ProviderError, raise_if_error_envelope


class TestProviderError:
    def test_stores_message(self):
        exc = ProviderError("something went wrong")
        assert str(exc) == "something went wrong"

    def test_stores_status_code(self):
        exc = ProviderError("rate limited", status_code=429)
        assert exc.status_code == 429

    def test_status_code_defaults_to_none(self):
        exc = ProviderError("connection refused")
        assert exc.status_code is None

    def test_is_exception(self):
        assert isinstance(ProviderError("x"), Exception)


class TestRaiseIfErrorEnvelope:
    def test_no_error_key_does_nothing(self):
        data = {"choices": [{"message": {"content": "hello"}}]}
        raise_if_error_envelope(data, "OpenRouter")  # should not raise

    def test_none_error_does_nothing(self):
        data = {"error": None, "choices": []}
        raise_if_error_envelope(data, "OpenRouter")  # should not raise

    def test_error_dict_with_message_and_int_code(self):
        data = {"error": {"message": "Model overloaded", "code": 529}}
        with pytest.raises(ProviderError) as exc_info:
            raise_if_error_envelope(data, "OpenRouter")
        err = exc_info.value
        assert "Model overloaded" in str(err)
        assert err.status_code == 529

    def test_error_dict_with_message_only(self):
        data = {"error": {"message": "Upstream failure"}}
        with pytest.raises(ProviderError) as exc_info:
            raise_if_error_envelope(data, "OpenRouter")
        err = exc_info.value
        assert "Upstream failure" in str(err)
        assert err.status_code is None

    def test_error_dict_with_non_int_code(self):
        data = {"error": {"message": "Bad request", "code": "invalid_param"}}
        with pytest.raises(ProviderError) as exc_info:
            raise_if_error_envelope(data, "OpenRouter")
        assert exc_info.value.status_code is None

    def test_error_string_value(self):
        data = {"error": "Internal server error"}
        with pytest.raises(ProviderError) as exc_info:
            raise_if_error_envelope(data, "OpenRouter")
        err = exc_info.value
        assert "Internal server error" in str(err)
        assert err.status_code is None

    def test_provider_label_in_message(self):
        data = {"error": {"message": "overloaded"}}
        with pytest.raises(ProviderError) as exc_info:
            raise_if_error_envelope(data, "MyProvider")
        assert "MyProvider" in str(exc_info.value)

    def test_error_dict_with_zero_code(self):
        # code=0 is an int — should be used as status_code
        data = {"error": {"message": "unknown", "code": 0}}
        with pytest.raises(ProviderError) as exc_info:
            raise_if_error_envelope(data, "Test")
        assert exc_info.value.status_code == 0
