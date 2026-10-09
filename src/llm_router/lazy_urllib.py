"""``urllib.request`` on first use.

Importing it costs ~7-9 ms (``http.client`` and the email parser; ``python -X
importtime``). A module whose HTTP call sits behind a gate, as ``direct_executor``'s
does for most prompts, binds ``urllib`` to this object instead of importing the
package at start-up: ``urllib.request.urlopen(...)`` resolves the real module on
first attribute access, and ``mod.urllib.request`` is still the real module for a
test that patches it.
"""

from __future__ import annotations


class _LazyUrllib:
    @property
    def request(self):
        import urllib.request as _request

        return _request


urllib = _LazyUrllib()
