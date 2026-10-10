"""Offline checks of the Terraform target declarations; never an OCI plan.

Run from the repository root with --engine local or --engine podman.
Only variables.tf and synthetic inputs are copied to disposable scratch.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
IMAGE = "docker.io/hashicorp/terraform@sha256:bbc41b888559ece9b3fce0ab834ed7120d898e1a808e4716c37b2d2efb4f2782"
VALUES = {"region": "us-ashburn-1", "compartment_ocid": "ocid1.compartment.oc1..synthetic"}


def check(engine):
    scratch = ROOT / ".cache" / "terraform-inputs"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        workspace = Path(directory)
        workspace.chmod(0o755)
        shutil.copyfile(ROOT / "terraform" / "variables.tf", workspace / "variables.tf")
        (workspace / "empty.tfrc").write_text("")
        if engine == "podman":
            command = ["podman", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
                       "--tmpfs=/tmp:rw,mode=1777", "--volume", f"{workspace}:/config:ro",
                       "--env=CHECKPOINT_DISABLE=1", "--env=TF_IN_AUTOMATION=1",
                       "--env=TF_CLI_CONFIG_FILE=/config/empty.tfrc", "--env=TF_DATA_DIR=/tmp/tfdata",
                       IMAGE, "-chdir=/config"]
            environment = None  # Host Podman connection only; no host env is passed to the container.
        else:
            command = ["terraform", f"-chdir={workspace}"]
            environment = {"PATH": os.defpath + os.pathsep + os.environ.get("PATH", ""),
                           "CHECKPOINT_DISABLE": "1", "TF_IN_AUTOMATION": "1",
                           "TF_CLI_CONFIG_FILE": str(workspace / "empty.tfrc"),
                           "TF_DATA_DIR": str(workspace / ".terraform")}

        def run(*arguments, succeeds=True, diagnostic=None):
            result = subprocess.run(command + list(arguments), env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            if (result.returncode == 0) != succeeds or (diagnostic and diagnostic not in output):
                raise RuntimeError(f"Terraform input check failed: {arguments[0]}\n{output}")
            return result.stdout

        version = json.loads(run("version", "-json"))["terraform_version"]
        if engine == "podman" and version != "1.5.7":
            raise RuntimeError("Unexpected pinned Terraform version")
        run("fmt", "-check", "-no-color")
        run("init", "-backend=false", "-input=false", "-no-color")
        run("validate", "-no-color")
        cases = [(VALUES, True, None),
                 ({**VALUES, "region": "us-phoenix-1"}, True, None),
                 ({**VALUES, "region": "bad-region"}, False, "Set region"),
                 ({**VALUES, "region": "us-ashburn-1\n"}, False, "Set region"),
                 ({**VALUES, "region": "us-" + "a" * 60 + "-1"}, False, "Set region"),
                 ({**VALUES, "region": None}, False, "required variable may not be set to null"),
                 ({**VALUES, "compartment_ocid": "demo-compartment"}, False, "Set compartment_ocid"),
                 ({**VALUES, "compartment_ocid": "ocid1.tenancy.oc1..synthetic"}, False, "Set compartment_ocid"),
                 ({**VALUES, "compartment_ocid": "ocid1.compartment.oc2..synthetic"}, False, "Set compartment_ocid"),
                 ({**VALUES, "compartment_ocid": "ocid1.compartment.oc1.." + "x" * 256}, False, "Set compartment_ocid"),
                 ({**VALUES, "compartment_ocid": None}, False, "required variable may not be set to null")]
        example = json.loads((ROOT / "terraform" / "deployment.tfvars.json.example").read_text())
        cases.append((example, False, "Set compartment_ocid"))
        for values, succeeds, diagnostic in cases:
            (workspace / "deployment.tfvars.json").write_text(json.dumps(values))
            run("plan", "-input=false", "-lock=false", "-refresh=false", "-no-color",
                "-var-file=deployment.tfvars.json", succeeds=succeeds, diagnostic=diagnostic)
    print(f"PASS: Terraform {version}, {engine}, fmt/init/validate and {len(cases)} input cases; no resources/providers.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("local", "podman"), required=True)
    check(parser.parse_args().engine)
