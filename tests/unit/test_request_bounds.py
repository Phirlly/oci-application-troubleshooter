import asyncio
import unittest

from starlette.requests import Request

from troubleshooter_app.web import login_password


class RequestBoundsTests(unittest.IsolatedAsyncioTestCase):
    def request(self, receive, *, encoding=None):
        headers = [(b'content-type', b'application/json')]
        if encoding:
            headers.append((b'content-encoding', encoding))
        return Request({'type': 'http', 'headers': headers}, receive)

    async def test_body_read_has_a_deadline(self):
        async def receive():
            await asyncio.sleep(20)
            raise AssertionError('body deadline was not enforced')

        with self.assertRaises(TimeoutError):
            await login_password(self.request(receive))

    async def test_streamed_body_limit_and_encoding_refusal(self):
        async def receive():
            return {'type': 'http.request', 'body': b'x' * 2048, 'more_body': True}

        with self.assertRaises(ValueError):
            await login_password(self.request(receive))

        async def must_not_read():
            raise AssertionError('unsupported content encoding must be refused before reading')

        with self.assertRaises(ValueError):
            await login_password(self.request(must_not_read, encoding=b'gzip'))
