import copy
import unittest
from unittest.mock import patch

from troubleshooter_deployment.oci_transport import BoundedOciSession, TransportError
from tests.contract.oci_responses import response


class OciTransportTests(unittest.TestCase):
    def setUp(self):
        self.session = BoundedOciSession("https://objectstorage.us-ashburn-1.oraclecloud.com")
        self.addCleanup(self.session.close)
        self.send = patch("oci.base_client.OCIHTTPAdapter.send").start()
        self.addCleanup(patch.stopall)

    def request(self, session=None):
        return (session or self.session).request("GET", "https://objectstorage.us-ashburn-1.oraclecloud.com/n/synthetic")

    def test_tls_streaming_no_proxy_and_closed_bounded_cache(self):
        reply = response({"synthetic": True})
        self.send.return_value = reply
        result = self.request()
        self.assertTrue(reply.raw.closed)
        self.assertEqual(result.json(), {"synthetic": True})
        self.assertTrue(self.send.call_args.kwargs["stream"])
        self.assertIs(self.send.call_args.kwargs["verify"], True)
        self.assertFalse(self.session.trust_env)
        self.assertEqual(self.send.call_args.kwargs["proxies"], {})
        self.assertEqual(self.send.call_args.kwargs["timeout"], (5, 5))

    def test_redirect_is_rejected_before_redirect_body_consumption(self):
        reply = response(body=b"synthetic", status=302, headers={"Location": "https://untrusted.invalid"})
        reply.raw.read = lambda *args, **kwargs: self.fail("Redirect body was consumed")
        self.send.return_value = reply
        with self.assertRaises(TransportError):
            self.request()
        self.send.assert_called_once()
        self.assertTrue(reply.raw.closed)

    def test_body_limits_encoding_json_and_truncation_apply_to_errors_too(self):
        cases = [
            (b"x" * 262145, {}), (b"{}", {"Content-Length": "900000"}),
            (b"{}", {"Content-Length": "3"}), (b"{}", {"Content-Length": "bad"}),
            (b"{}", {"Content-Encoding": "gzip"}), (b'{"a":1,"a":2}', {}), (b"NaN", {}),
            (b"{", {}), (b"[" * 2000, {}),
        ]
        for status in [200, 500]:
            for body, headers in cases:
                reply = response(body=body, status=status, headers=headers)
                self.send.return_value = reply
                with self.assertRaises(TransportError):
                    self.request()
                self.assertTrue(reply.raw.closed)

    def test_session_copy_retains_origin_limits_and_prevents_override(self):
        copied = copy.copy(self.session)
        self.addCleanup(copied.close)
        for url in ["http://objectstorage.us-ashburn-1.oraclecloud.com/n/a", "https://untrusted.invalid/n/a"]:
            with self.assertRaises(TransportError):
                copied.request("GET", url)
        self.send.assert_not_called()
        self.send.return_value = response(body=b"x" * 262145)
        with self.assertRaises(TransportError):
            self.request(copied)

    def test_untrusted_content_type_cannot_bypass_strict_json(self):
        for content_type in [None, "text/plain", "application/octet-stream"]:
            self.send.return_value = response(body=b'{"id":"other","id":"expected"}',
                                              headers={"Content-Type": content_type})
            with self.assertRaises(TransportError):
                self.request()

    def test_failed_response_close_is_sanitized(self):
        reply = response()
        original = reply.close
        def broken_close():
            original()
            raise OSError("synthetic-secret")
        reply.close = broken_close
        self.send.return_value = reply
        with self.assertRaises(TransportError) as error:
            self.request()
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertTrue(reply.raw.closed)

    def test_interrupted_raw_body_is_closed(self):
        reply = response()
        def broken(*args, **kwargs):
            raise OSError("synthetic-secret")
        reply.raw.read = broken
        self.send.return_value = reply
        with self.assertRaises(TransportError) as error:
            self.request()
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertTrue(reply.raw.closed)
