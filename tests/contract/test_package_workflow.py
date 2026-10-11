"""Keep public package validation independent of credentialed deployment."""
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PackageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = json.loads((ROOT / '.github/workflows/package.yml').read_text())
        self.job = self.workflow['jobs']['package']

    def test_unprivileged_branch_checks_with_bounded_execution(self):
        self.assertEqual(self.workflow['on'], {
            'push': {'branches': ['dev', 'main']},
            'pull_request': {'branches': ['dev', 'main']}})
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        self.assertEqual(set(self.workflow['jobs']), {'package', 'terraform'})
        self.assertEqual(self.job['runs-on'], 'ubuntu-24.04')
        self.assertLessEqual(self.job['timeout-minutes'], 20)
        self.assertNotIn('permissions', self.job)
        self.assertEqual(self.workflow['concurrency'], {
            'group': 'package-${{ github.ref }}', 'cancel-in-progress': True})
        for forbidden in ('secrets.', 'environment', 'continue-on-error', 'workflow_dispatch',
                          'pull_request_target', 'deployments', 'id-token', 'upload-artifact'):
            self.assertNotIn(forbidden, json.dumps(self.workflow))
        for step in self.job['steps']:
            self.assertNotIn('if', step)
            if 'run' in step:
                self.assertNotIn('${{', step['run'])
                self.assertLessEqual(step['timeout-minutes'], 8)

    def test_terraform_job_is_bounded_and_has_no_deployment_authority(self):
        job = self.workflow['jobs']['terraform']
        self.assertEqual(job['runs-on'], 'ubuntu-24.04')
        self.assertLessEqual(job['timeout-minutes'], 15)
        self.assertNotIn('permissions', job)
        steps = job['steps']
        self.assertEqual([step['uses'] for step in steps if 'uses' in step], [
            'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
            'astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9'])
        self.assertEqual(steps[0]['with'], {'ref': '${{ github.sha }}', 'persist-credentials': False})
        self.assertEqual(steps[1]['with'], {'version': '0.13.0', 'python-version': '3.12.15', 'enable-cache': False})
        commands = [step['run'] for step in steps if 'run' in step]
        self.assertEqual(commands, [
            'uv sync --locked --no-build --no-dev --python 3.12.15',
            '.venv/bin/python tests/integration/check_workload_terraform.py --pull-image',
            'git diff --exit-code -- terraform pyproject.toml uv.lock'])
        for step in steps:
            self.assertNotIn('if', step)
            if 'run' in step:
                self.assertNotIn('${{', step['run'])
                self.assertLessEqual(step['timeout-minutes'], 12)

    def test_exact_tools_without_credential_or_cache_persistence(self):
        steps = self.job['steps']
        self.assertEqual([s['uses'] for s in steps if 'uses' in s], [
            'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
            'astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9',
            'actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1'])
        self.assertEqual(steps[0]['with'], {'ref': '${{ github.sha }}', 'persist-credentials': False})
        self.assertEqual(steps[1]['with'], {'version': '0.13.0', 'python-version': '3.12.15', 'enable-cache': False})
        self.assertEqual(steps[2]['with'], {'node-version': '24.21.0', 'architecture': 'x64', 'package-manager-cache': False})
        self.assertEqual(self.job['env']['NPM_CONFIG_USERCONFIG'], '/dev/null')
        self.assertEqual(self.job['env']['NPM_CONFIG_REGISTRY'], 'https://registry.npmjs.org')

    def test_locked_build_browser_and_real_image_are_required(self):
        steps = {s['id']: s for s in self.job['steps'] if 'id' in s}
        self.assertEqual(steps['python_dependencies']['run'], 'uv sync --locked --no-build --python 3.12.15')
        self.assertIn('coverage run -m unittest discover -s tests', steps['python_checks']['run'])
        self.assertIn('coverage report --fail-under=98', steps['python_checks']['run'])
        self.assertEqual(steps['python_audit']['run'], 'uv audit --frozen')
        self.assertEqual(steps['frontend_dependencies']['run'], 'npm ci --ignore-scripts --no-audit --no-fund')
        self.assertEqual(steps['frontend_build']['run'], 'npm run typecheck\nnpm run build\nnode ../container/frontend_notices.mjs')
        self.assertEqual(steps['frontend_audit']['run'], 'npm audit --audit-level=low')
        self.assertEqual(steps['browser_install']['run'], './node_modules/.bin/playwright install --with-deps chromium')
        self.assertEqual(steps['browser_tests']['run'], 'npm run test:browser -- --forbid-only --global-timeout=180000')
        for name in ('frontend_dependencies', 'frontend_build', 'frontend_audit', 'browser_install', 'browser_tests'):
            self.assertEqual(steps[name]['working-directory'], 'frontend')
        self.assertEqual(steps['image_build']['run'], 'podman build --platform linux/amd64 --no-cache --iidfile .cache/presenter-image.id --file Containerfile .')
        self.assertEqual(steps['image_smoke']['run'], '.venv/bin/python tests/integration/check_presenter_image.py --image-id-file .cache/presenter-image.id')
        self.assertEqual(steps['unchanged_inputs']['run'], 'git diff --exit-code -- pyproject.toml uv.lock frontend/package.json frontend/package-lock.json')
        self.assertFalse((ROOT / '.github/workflows/frontend-lock.yml').exists())
