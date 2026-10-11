import json
from pathlib import Path
import unittest


class DemoWorkflowTests(unittest.TestCase):
    def test_manual_trusted_release_permissions_and_serialization(self):
        workflow = json.loads((Path(__file__).resolve().parents[2] / ".github/workflows/demo.yml").read_text())
        self.assertEqual(workflow["on"], {"workflow_dispatch": {}})
        self.assertEqual(workflow["permissions"], {})
        self.assertEqual(workflow["concurrency"], {"group": "oci-application-troubleshooter-demo", "cancel-in-progress": False})
        self.assertEqual(set(workflow["jobs"]), {"bootstrap"})
        job = workflow["jobs"]["bootstrap"]
        self.assertEqual(job["environment"], "demo")
        self.assertEqual(job["if"], "${{ github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' }}")
        self.assertEqual(job["permissions"], {"contents": "read", "actions": "read", "deployments": "write"})
        self.assertEqual(job["runs-on"], "ubuntu-24.04")
        self.assertLessEqual(job["timeout-minutes"], 10)
        steps = job["steps"]
        self.assertEqual([s["uses"] for s in steps if "uses" in s], [
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9"])
        self.assertEqual(steps[0]["with"], {"ref": "${{ github.sha }}", "persist-credentials": False})
        self.assertEqual(steps[1]["with"], {"version": "0.12.19", "python-version": "3.12.15", "enable-cache": False})
        self.assertNotIn("secrets.", json.dumps(steps[:-1]))
        self.assertEqual(set(steps[-1]["env"]), {"GITHUB_TOKEN", "DEPLOYMENT_TFVARS_JSON", "OCI_TENANCY_OCID",
                                               "OCI_USER_OCID", "OCI_FINGERPRINT", "OCI_PRIVATE_KEY"})
        for name, value in steps[-1]["env"].items():
            self.assertEqual(value, "${{ github.token }}" if name == "GITHUB_TOKEN" else "${{ secrets." + name + " }}")
        commands = [step["run"] for step in steps if "run" in step]
        self.assertEqual(commands[-1], ".venv/bin/python -B -m troubleshooter_deployment.github_runner")
        self.assertIn("--locked --no-build --no-dev", commands[0])
        self.assertTrue(all("${{" not in command for command in commands))
        self.assertNotIn("env", workflow)
        self.assertNotIn("env", job)
        self.assertNotIn("Demo ready", json.dumps(workflow))
