"""Guard the temporary public-only lock acquisition publication boundary."""
import json
from pathlib import Path
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[2]


class FrontendLockWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads((ROOT / '.github/workflows/frontend-lock.yml').read_text())

    def test_dev_only_read_permissions_without_deployment_access(self):
        workflow = self.workflow
        self.assertEqual(workflow['on'], {'push': {'branches': ['dev'], 'paths': [
            '.github/workflows/frontend-lock.yml', 'frontend/package.json',
            'tests/contract/test_frontend_lock_workflow.py']}})
        self.assertEqual(workflow['permissions'], {'contents': 'read'})
        self.assertEqual(set(workflow['jobs']), {'resolve'})
        job = workflow['jobs']['resolve']
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertLessEqual(job['timeout-minutes'], 10)
        self.assertEqual(job['if'], "${{ github.event_name == 'push' && github.ref == 'refs/heads/dev' }}")
        serialized = json.dumps(workflow)
        for forbidden in ('secrets.', 'environment', 'continue-on-error', 'github.token',
                          'workflow_dispatch', 'deployments', 'id-token', 'tls-verify=false'):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn('permissions', job)

    def test_pinned_checkout_and_success_only_single_file_artifact(self):
        steps = self.workflow['jobs']['resolve']['steps']
        self.assertEqual([s['uses'] for s in steps if 'uses' in s], [
            'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
            'actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9'])
        self.assertEqual(steps[0]['with'], {'ref': '${{ github.sha }}', 'persist-credentials': False})
        self.assertEqual(steps[-1]['with'], {'name': 'frontend-lock',
            'path': 'frontend/package-lock.json', 'retention-days': 1,
            'if-no-files-found': 'error', 'include-hidden-files': False})
        self.assertNotIn('if', steps[-1])
        self.assertTrue(any(s.get('run') == 'test ! -e frontend/package-lock.json' for s in steps))

    def test_isolated_bounded_resolution_and_audit(self):
        commands = [s['run'] for s in self.workflow['jobs']['resolve']['steps']
                    if s.get('run', '').startswith('podman run ')]
        self.assertEqual(len(commands), 2)
        for command, duration, mode in zip(commands, ('300', '60'), ('rw', 'ro'), strict=True):
            args = shlex.split(command)
            self.assertEqual(args[:3], ['podman', 'run', '--rm'])
            for flag, value in (('--timeout', duration), ('--userns', 'keep-id'),
                                ('--cap-drop', 'ALL'), ('--security-opt', 'no-new-privileges'),
                                ('--volume', '$GITHUB_WORKSPACE/frontend:/frontend:' + mode)):
                self.assertEqual(args[args.index(flag) + 1], value)
            self.assertEqual(args.count('--volume'), 1)
            self.assertIn('--read-only', args)
            self.assertIn('NPM_CONFIG_USERCONFIG=/dev/null', args)
            self.assertIn('NPM_CONFIG_CACHE=/tmp/npm', args)
            self.assertIn('docker.io/library/node@sha256:51b1100cc2a83d370c6a60952e3f2989c8a43159d0e38586e090f3b3326efefd', args)
            self.assertIn('--package-lock-only', args)
            self.assertIn('--ignore-scripts', args)
            self.assertIn('--registry=https://registry.npmjs.org', args)
            self.assertIn('--fetch-retries=1', args)
            self.assertIn('--fetch-timeout=20000', args)
            self.assertNotIn('${{', command)
        self.assertIn('npm install ', commands[0])
        self.assertIn('npm audit ', commands[1])
        self.assertIn('--audit-level=low', commands[1])
        self.assertNotIn(' fix', commands[1])
