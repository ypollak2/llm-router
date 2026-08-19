"""G-F Group B — `router._extract_retry_after`, all 27 mutants.

The function reads a `Retry-After` header off two different exception shapes and returns
seconds, or None. Nothing tested it, so every branch survived: the two attribute names,
the header lookup, the int conversion, and the swallow.

WHY IT MATTERS BEYOND THE SCORE
-------------------------------
This value decides how long the router waits before retrying a rate-limited provider.
Returning None where a header existed means backing off by the default instead of the
value the provider asked for — retrying too early, getting rate-limited again, and
burning quota. Returning a wrong number is worse: it looks like the provider's own
instruction.

The two branches are asserted SEPARATELY throughout. `http_response` and `_response` are
different attribute names on different exception shapes, and a mutant that reads one
where it should read the other is invisible to any test that only supplies one of them.
"""

from __future__ import annotations

import pytest

from llm_router.router import _extract_retry_after


class _Response:
    def __init__(self, headers):
        self.headers = headers


class _HttpResponseError(Exception):
    """LiteLLM shape: the header hangs off `.http_response`."""

    def __init__(self, headers):
        super().__init__("rate limited")
        self.http_response = _Response(headers)


class _UnderscoreResponseError(Exception):
    """Fallback shape: the header hangs off `._response`."""

    def __init__(self, headers):
        super().__init__("rate limited")
        self._response = _Response(headers)


class TestTheHttpResponseBranch:
    def test_a_retry_after_header_is_returned_as_an_int(self):
        assert _extract_retry_after(_HttpResponseError({"retry-after": "30"})) == 30

    def test_the_value_is_converted_not_passed_through_as_a_string(self):
        """`int(val)` — the caller does arithmetic on this. A string would raise or,
        worse, silently concatenate somewhere downstream."""
        out = _extract_retry_after(_HttpResponseError({"retry-after": "45"}))
        assert out == 45
        assert isinstance(out, int)

    def test_an_integer_header_value_also_works(self):
        assert _extract_retry_after(_HttpResponseError({"retry-after": 12})) == 12

    def test_a_missing_retry_after_key_yields_None(self):
        assert _extract_retry_after(_HttpResponseError({"x-other": "30"})) is None

    def test_empty_headers_yield_None(self):
        assert _extract_retry_after(_HttpResponseError({})) is None

    def test_the_header_name_is_matched_exactly(self):
        """`'retry-after' in headers` — the literal drives the lookup.

        A mutated key finds nothing and the function reports "no backoff requested"
        while the provider is asking for one.
        """
        assert _extract_retry_after(_HttpResponseError({"retryafter": "30"})) is None
        assert _extract_retry_after(_HttpResponseError({"retry_after": "30"})) is None


class TestTheUnderscoreResponseBranch:
    """A SEPARATE attribute name — asserted independently on purpose.

    A mutant swapping `_response` for `http_response` (or vice versa) passes any test
    that only ever supplies one shape.
    """

    def test_a_retry_after_header_is_returned_as_an_int(self):
        assert _extract_retry_after(_UnderscoreResponseError({"retry-after": "60"})) == 60

    def test_a_missing_key_yields_None(self):
        assert _extract_retry_after(_UnderscoreResponseError({"x-other": "60"})) is None

    def test_this_shape_is_reached_only_when_http_response_is_absent(self):
        """Pins the ORDER: `http_response` is checked first and wins.

        An exception carrying both must take the http_response value, so a mutant
        reordering the branches returns the wrong header.
        """
        exc = _HttpResponseError({"retry-after": "10"})
        exc._response = _Response({"retry-after": "999"})
        assert _extract_retry_after(exc) == 10


class TestMalformedInputIsSwallowed:
    """`except (ValueError, TypeError, AttributeError): pass` then `return None`.

    The contract is total: this must never raise into the retry path, because an
    exception here would replace a rate-limit error with a crash.
    """

    def test_a_non_numeric_header_yields_None_rather_than_raising(self):
        assert _extract_retry_after(_HttpResponseError({"retry-after": "soon"})) is None

    def test_a_None_header_value_yields_None(self):
        assert _extract_retry_after(_HttpResponseError({"retry-after": None})) is None

    def test_an_exception_with_neither_attribute_yields_None(self):
        assert _extract_retry_after(ValueError("nothing to see")) is None

    def test_headers_that_are_not_a_mapping_yield_None(self):
        """`getattr(..., 'headers', {})` can return anything. `in` on a non-container
        raises TypeError, which the handler must absorb."""
        exc = _HttpResponseError({})
        exc.http_response.headers = 42
        assert _extract_retry_after(exc) is None

    def test_a_response_object_without_headers_yields_None(self):
        """The `{}` default on the getattr — without it this raises AttributeError
        before the handler is reached on some shapes."""
        exc = _HttpResponseError({})
        del exc.http_response.headers
        assert _extract_retry_after(exc) is None

    def test_a_headerless_http_response_FALLS_THROUGH_to_the_underscore_branch(self):
        """The `{}` default is what makes the fallback reachable.

        With the default, a `http_response` carrying no `headers` yields `{}`, the key
        lookup misses, and execution CONTINUES to the `_response` branch. Without it,
        `getattr` raises AttributeError, the handler swallows it, and the function
        returns None — never trying the second shape at all.

        Both spellings return None when only the first shape exists, which is why the
        earlier headerless test could not tell them apart. Only an exception carrying a
        headerless `http_response` AND a valid `_response` separates them.
        """
        exc = _HttpResponseError({})
        del exc.http_response.headers
        exc._response = _Response({"retry-after": "77"})
        assert _extract_retry_after(exc) == 77, (
            "a headerless first shape must not abort the lookup; the second shape "
            "still carries the provider's instruction"
        )

    def test_a_float_string_yields_None_not_a_truncated_int(self):
        """`int("1.5")` raises ValueError. Returning None is correct — silently
        truncating a fractional backoff would be inventing a value."""
        assert _extract_retry_after(_HttpResponseError({"retry-after": "1.5"})) is None


class TestValuesArePreservedNotDefaulted:
    """A mutant returning a constant would satisfy any single-value assertion."""

    @pytest.mark.parametrize("seconds", [0, 1, 30, 120, 3600])
    def test_each_distinct_value_round_trips(self, seconds):
        assert _extract_retry_after(
            _HttpResponseError({"retry-after": str(seconds)})
        ) == seconds

    def test_zero_is_returned_as_zero_not_as_None(self):
        """`return int(val)` with val="0". A mutant treating falsy as absent would
        report "no header" for a provider explicitly asking for an immediate retry."""
        assert _extract_retry_after(_HttpResponseError({"retry-after": "0"})) == 0
