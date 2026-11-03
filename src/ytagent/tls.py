"""Fast CA-bundle loading.

On this PC OpenSSL takes 11-17 s to load certifi's bundle from its file path (the same certificates passed as
text load in 0.1 s). httpx and requests/urllib3 load it for every new client or connection, so each preview
fetch took 12-45 s, and urllib3 - which loads it after the TCP connection is open - sat silent long enough
for Google to drop the connection ("SSL: UNEXPECTED_EOF_WHILE_READING").

`install()` makes SSLContext.load_verify_locations(cafile=<.pem>) read the file once and pass it as text.
Verification is unchanged: same certificates, same checks.
"""

from __future__ import annotations

import functools
import os
import ssl

_original = ssl.SSLContext.load_verify_locations


@functools.cache
def _pem(path: str) -> str:
    with open(path, encoding="ascii") as fh:
        return fh.read()


def _load_verify_locations(self, cafile=None, capath=None, cadata=None):
    if cafile is not None and capath is None and cadata is None and os.fspath(cafile).endswith(".pem"):
        return _original(self, cadata=_pem(os.fspath(cafile)))
    return _original(self, cafile, capath, cadata)


def install() -> None:
    ssl.SSLContext.load_verify_locations = _load_verify_locations
