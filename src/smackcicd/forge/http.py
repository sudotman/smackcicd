# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP plumbing shared by the forge clients (stdlib only)."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .. import __version__

USER_AGENT = "smackcicd/%s" % __version__


class ForgeError(RuntimeError):
    def __init__(self, status, message, url=""):
        self.status = status
        self.url = url
        super().__init__("%s: %s (%s)" % (status or "network", message, url))


class ChainedReader:
    """A file-like body that streams several parts in sequence.

    Lets a multi-gigabyte artifact upload without being loaded into memory.
    """

    def __init__(self, parts):
        self._parts = list(parts)
        self._index = 0

    def read(self, size=-1):
        chunks = []
        remaining = size
        while self._index < len(self._parts):
            part = self._parts[self._index]
            data = part.read(remaining) if remaining and remaining > 0 else part.read()
            if not data:
                self._close(part)
                self._index += 1
                continue
            chunks.append(data)
            if remaining and remaining > 0:
                remaining -= len(data)
                if remaining <= 0:
                    break
            elif size != -1:
                break
        return b"".join(chunks)

    @staticmethod
    def _close(part):
        closer = getattr(part, "close", None)
        if closer:
            closer()

    def close(self):
        for part in self._parts[self._index:]:
            self._close(part)


def upload_timeout(base, size):
    """Read timeout for an upload's response.

    Forges reply only once the whole file is stored -- on a NAS that can mean
    two full writes of a multi-GB file. This is a ceiling, not an estimate:
    ten minutes plus a pessimistic 2 MB/s.
    """
    return max(base, 600) + size / (2 * 1024 * 1024)


class Http:
    def __init__(self, token="", auth_scheme="token", timeout=120, verify_tls=True,
                 extra_headers=None):
        self.token = token
        self.auth_scheme = auth_scheme
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self._ctx = None if verify_tls else ssl._create_unverified_context()

    def request(self, method, url, query=None, body=None, headers=None, raw_body=None,
                content_length=None, parse=True, timeout=None, want_headers=False):
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        head = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        head.update(self.extra_headers)
        if self.token:
            head["Authorization"] = "%s %s" % (self.auth_scheme, self.token)
        if body is not None:
            raw_body = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
            content_length = len(raw_body)
        head.update(headers or {})
        if content_length is not None:
            head["Content-Length"] = str(content_length)

        req = urllib.request.Request(url, data=raw_body, headers=head, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout,
                                        context=self._ctx) as resp:
                payload = resp.read()
                response_headers = dict(resp.headers.items())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise ForgeError(exc.code, detail or exc.reason, url) from exc
        except urllib.error.URLError as exc:
            raise ForgeError(0, str(exc.reason), url) from exc
        except OSError as exc:
            # A socket timeout mid-response is not wrapped in URLError, and it
            # must not escape: one slow upload would abort the whole job.
            raise ForgeError(0, "%s: %s" % (type(exc).__name__, exc), url) from exc
        data = None
        if parse and payload:
            try:
                data = json.loads(payload.decode("utf-8"))
            except ValueError:
                data = None
        return (data, response_headers) if want_headers else data
