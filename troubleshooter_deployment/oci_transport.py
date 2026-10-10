"""Bound the pinned OCI SDK before deserialization or redirect processing."""
import json
from contextlib import contextmanager
import os
import re
from urllib.parse import urlsplit

from oci._vendor.requests import Session
from oci.base_client import OCIHTTPAdapter

from .codec import MAX_BYTES
from .request_budget import operation_budget


class TransportError(RuntimeError):
    def __init__(self):
        super().__init__("oci_transport_rejected")


def strong_etag(value):
    if (type(value) is not str or not 0 < len(value) <= 1024 or value.startswith("W/")
            or value == "*" or not re.fullmatch(r'"[!#-+\--~]+"|[!#-+\--~]+', value)):
        raise TransportError()
    return value


@contextmanager
def oci_operation():
    # NoneRetryStrategy does not disable this independent BaseClient retry path.
    key = "OCI_HEADER_PARSING_ERROR_MAX_RETRIES"
    with operation_budget():
        previous = os.environ.get(key)
        os.environ[key] = "0"
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransportError()
        result[key] = value
    return result


def _constant(value):
    raise TransportError()


def _bounded_response(response, **kwargs):
    try:
        # Requests processes redirects (including their bodies) after hooks.
        if 300 <= response.status_code < 400 or response.headers.get("Content-Encoding") not in (None, "identity"):
            raise TransportError()
        length = response.headers.get("Content-Length")
        if length is not None and (not re.fullmatch(r"[0-9]{1,9}", length) or int(length) > MAX_BYTES):
            raise TransportError()
        body = response.raw.read(MAX_BYTES + 1, decode_content=False)
        if len(body) > MAX_BYTES or (length is not None and len(body) != int(length)):
            raise TransportError()
        # Every nonempty response in the bucket/journal/receipt protocols is
        # JSON. BaseClient deserializes models regardless of the media type.
        if body:
            json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
    except Exception:
        raise TransportError() from None
    finally:
        try:
            response.close()
        except Exception:
            raise TransportError() from None
    # The vendored SDK subsequently consumes .content; only bounded bytes remain.
    response._content = body
    response._content_consumed = True
    return response


class BoundedOciSession(Session):
    # BaseClient copies sessions on certain errors. Preserve the origin on copy.
    __attrs__ = Session.__attrs__ + ["_origin"]

    def __init__(self, origin):
        super().__init__()
        self._origin = origin
        self.trust_env = False
        self.mount("https://", OCIHTTPAdapter())

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self._origin or parsed.scheme != "https":
            raise TransportError()
        kwargs.update(allow_redirects=False, stream=True, verify=True, timeout=(5, 5), proxies={},
                      hooks={"response": [_bounded_response]})
        kwargs["headers"] = dict(kwargs.get("headers") or {}, **{"Accept-Encoding": "identity"})
        result = super().request(method, url, **kwargs)
        # GetNamespace's SDK string special case preserves non-string JSON as
        # text even with skip_deserialization. Check the bounded wire value.
        if method == "GET" and parsed.path == "/n" and 200 <= result.status_code < 300:
            if type(json.loads(result.content)) is not str:
                raise TransportError()
        return result
