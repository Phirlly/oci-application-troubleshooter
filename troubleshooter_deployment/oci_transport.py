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


TRANSPORT_REASONS = (
    "response_headers", "redirect", "content_encoding", "content_length_invalid",
    "content_length_limit", "body_read", "body_limit", "content_length_mismatch",
    "json_duplicate_key", "json_nonfinite", "json_invalid", "response_close",
)


class TransportError(RuntimeError):
    def __init__(self, reason=None):
        self.reason = reason if type(reason) is str and reason in TRANSPORT_REASONS else "unspecified"
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
            raise TransportError("json_duplicate_key")
        result[key] = value
    return result


def _constant(value):
    raise TransportError("json_nonfinite")


def _bounded_response(response, **kwargs):
    failure, reason = None, "response_headers"
    try:
        # Requests processes redirects (including their bodies) after hooks.
        if 300 <= response.status_code < 400:
            raise TransportError("redirect")
        if response.headers.get("Content-Encoding") not in (None, "identity"):
            raise TransportError("content_encoding")
        length = response.headers.get("Content-Length")
        if length is not None:
            if type(length) is not str or not re.fullmatch(r"[0-9]{1,9}", length):
                raise TransportError("content_length_invalid")
            if int(length) > MAX_BYTES:
                raise TransportError("content_length_limit")
        reason = "body_read"
        body = response.raw.read(MAX_BYTES + 1, decode_content=False)
        if len(body) > MAX_BYTES:
            raise TransportError("body_limit")
        if length is not None and len(body) != int(length):
            raise TransportError("content_length_mismatch")
        # Every nonempty response in the bucket/journal/receipt protocols is
        # JSON. BaseClient deserializes models regardless of the media type.
        if body:
            reason = "json_invalid"
            json.loads(body, object_pairs_hook=_pairs, parse_constant=_constant)
    except TransportError as error:
        failure = TransportError(error.reason)
    except Exception:
        failure = TransportError(reason)
    finally:
        try:
            response.close()
        except Exception:
            if failure is None:
                failure = TransportError("response_close")
    if failure is not None:
        raise failure from None
    # The vendored SDK subsequently consumes .content; only bounded bytes remain.
    response._content = body
    response._content_consumed = True
    return response


class BoundedOciSession(Session):
    # BaseClient copies sessions on certain errors. Preserve all bounds on copy.
    __attrs__ = Session.__attrs__ + ["_origin", "_read_seconds"]

    def __init__(self, origin, *, read_seconds=5):
        if type(read_seconds) is not int or read_seconds not in (5, 15):
            raise TransportError()
        super().__init__()
        self._origin = origin
        self._read_seconds = read_seconds
        self.trust_env = False
        self.mount("https://", OCIHTTPAdapter())

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        if f"{parsed.scheme}://{parsed.netloc}" != self._origin or parsed.scheme != "https":
            raise TransportError()
        kwargs.update(allow_redirects=False, stream=True, verify=True, timeout=(5, self._read_seconds), proxies={},
                      hooks={"response": [_bounded_response]})
        kwargs["headers"] = dict(kwargs.get("headers") or {}, **{"Accept-Encoding": "identity"})
        result = super().request(method, url, **kwargs)
        # GetNamespace's SDK string special case preserves non-string JSON as
        # text even with skip_deserialization. Check the bounded wire value.
        if method == "GET" and parsed.path == "/n" and 200 <= result.status_code < 300:
            if type(json.loads(result.content)) is not str:
                raise TransportError()
        return result
