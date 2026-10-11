from contextlib import redirect_stderr
import io
import unittest
from unittest.mock import patch

from troubleshooter_app.__main__ import main


class StartupTests(unittest.TestCase):
    def test_invalid_configuration_emits_only_fixed_error_and_does_not_serve(self):
        output = io.StringIO()
        with (patch('troubleshooter_app.__main__.create_app', side_effect=ValueError('private-fixture')),
              patch('troubleshooter_app.__main__.uvicorn.run') as serve, redirect_stderr(output)):
            self.assertEqual(main(), 1)
        serve.assert_not_called()
        self.assertEqual(output.getvalue(), 'Application startup failed: check deployment configuration and storage.\n')

    def test_runtime_uses_single_process_without_forwarded_identity_or_access_logs(self):
        app = object()
        with patch('troubleshooter_app.__main__.create_app', return_value=app), patch('troubleshooter_app.__main__.uvicorn.run') as serve:
            self.assertEqual(main(), 0)
        self.assertIs(serve.call_args.args[0], app)
        self.assertEqual(serve.call_args.kwargs['workers'], 1)
        self.assertFalse(serve.call_args.kwargs['proxy_headers'])
        self.assertFalse(serve.call_args.kwargs['access_log'])
        self.assertEqual(serve.call_args.kwargs['limit_concurrency'], 16)
