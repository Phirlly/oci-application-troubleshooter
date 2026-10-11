"""Validate the generated resource-free journal ZIP; no OCI calls or state."""
import argparse
from base64 import b64decode
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
from zipfile import ZipFile

from troubleshooter_deployment.stack_journal_config import declaration_zip
from troubleshooter_deployment.stack_journal_variables import KEYS
from tests.contract.check_terraform_inputs import IMAGE, ROOT


def check(engine):
    scratch = ROOT / ".cache" / "stack-journal-config"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        workspace = Path(directory)
        workspace.chmod(0o755)
        with ZipFile(BytesIO(b64decode(declaration_zip()))) as archive:
            source = archive.read("main.tf")
        inputs = {key: "synthetic" for key in KEYS | {"troubleshooter_initial_stack_v1"}}
        for name, body in (("main.tf", source), ("empty.tfrc", b""),
                           ("inputs.tfvars.json", json.dumps(inputs).encode())):
            path = workspace / name
            path.write_bytes(body)
            path.chmod(0o644)  # Synthetic inputs must work with host umask077.
        if engine == "podman":
            command = ["podman", "run", "--rm", "--pull=never", "--network=none", "--read-only",
                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
                       "--tmpfs=/tmp:rw,mode=1777", "--volume", f"{workspace}:/config:ro",
                       "--env=CHECKPOINT_DISABLE=1", "--env=TF_IN_AUTOMATION=1",
                       "--env=TF_CLI_CONFIG_FILE=/config/empty.tfrc", "--env=TF_DATA_DIR=/tmp/tfdata",
                       IMAGE, "-chdir=/config"]
            environment = None
        else:
            command = ["terraform", f"-chdir={workspace}"]
            environment = {"PATH": os.defpath + os.pathsep + os.environ.get("PATH", ""),
                           "CHECKPOINT_DISABLE": "1", "TF_IN_AUTOMATION": "1",
                           "TF_CLI_CONFIG_FILE": str(workspace / "empty.tfrc"),
                           "TF_DATA_DIR": str(workspace / ".terraform")}
        for arguments in (["version", "-json"], ["fmt", "-check", "-no-color"],
                          ["init", "-backend=false", "-input=false", "-no-color"],
                          ["validate", "-no-color"],
                          ["plan", "-input=false", "-lock=false", "-refresh=false", "-no-color",
                           "-var-file=inputs.tfvars.json"]):
            result = subprocess.run(command + arguments, env=environment, stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError("Synthetic journal ZIP check failed: " + result.stdout + result.stderr)
            if arguments[0] == "version":
                version = json.loads(result.stdout)["terraform_version"]
                if engine == "podman" and version != "1.5.7":
                    raise RuntimeError("Unexpected pinned Terraform version")
            if arguments[0] == "plan" and "No changes." not in result.stdout:
                raise RuntimeError("Journal ZIP must contain no resources")
    print(f"PASS: Terraform {version}, {engine}; generated journal ZIP fmt/init/validate/empty plan.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("local", "podman"), required=True)
    check(parser.parse_args().engine)
