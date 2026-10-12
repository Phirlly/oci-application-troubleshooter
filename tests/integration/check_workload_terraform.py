"""Opt-in real Terraform/OCI-provider checks, with OCI networking disabled.

Without --provider-directory, downloads the locked public provider first in a
separate credential-free container. Neither phase receives host OCI credentials.
Never applies a plan. Default unittest discovery does not execute this script.
"""
import argparse
from hashlib import md5
from io import BytesIO
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
from zipfile import ZipFile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from troubleshooter_deployment.workload_package import build_workload_package
from workload_fixture import VALUES, input_cases
from workload_plan_assertions import require, verify_plan

IMAGES = {
    "arm64": "docker.io/hashicorp/terraform@sha256:bbc41b888559ece9b3fce0ab834ed7120d898e1a808e4716c37b2d2efb4f2782",
    "amd64": "docker.io/hashicorp/terraform@sha256:c3bc74e7a2a8fab8216cbbedf12a9637db09288806a6aa537b6f397cba04dd93",
}


def podman(*arguments, timeout=45, succeeds=True, stdin=None):
    result = subprocess.run(["podman", *arguments], input=stdin,
                            capture_output=True, text=True, timeout=timeout)
    require((result.returncode == 0) == succeeds,
            "Podman/Terraform check failed:\n" + result.stdout[-12000:] + result.stderr[-12000:])
    return result.stdout if succeeds else result.stdout + result.stderr


def write(path, content):
    path.write_bytes(content if isinstance(content, bytes) else content.encode("ascii"))
    path.chmod(0o644)  # Synthetic public test files, also under umask077.


def cleanup(cidfiles):
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGTERM, signal.SIGINT})
    signal.alarm(0)
    failures = []
    try:
        for path in cidfiles:
            try:
                if path.exists():
                    identity = path.read_text().strip()
                    require(re.fullmatch(r"[a-f0-9]{64}", identity), "created container identity")
                    podman("rm", "--force", "--ignore", identity, timeout=30)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                failures.append(path.name)
        require(not failures, "Failed to remove owned Terraform containers: " + ", ".join(failures))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def verify(provider_directory, pull_image):
    architecture = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}.get(platform.machine())
    require(architecture in IMAGES, "unsupported native architecture")
    image = IMAGES[architecture]
    if pull_image:
        podman("pull", image, timeout=120)
    details = json.loads(podman("image", "inspect", image))[0]
    require(details["Architecture"] == architecture and details["Os"] == "linux", "native Terraform image")
    scratch = ROOT / ".cache" / "workload-terraform"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch) as directory:
        root = Path(directory)
        root.chmod(0o755)
        source = root / "source"
        source.mkdir(mode=0o755)
        source.chmod(0o755)
        package = build_workload_package(ROOT)
        with ZipFile(BytesIO(package.content)) as archive:
            for name in archive.namelist():
                write(source / name, archive.read(name))
        write(root / "empty.tfrc", "")
        cidfiles = [root / "acquisition.cid", root / "offline.cid"]
        common = ["--pull=never", "--read-only", "--cap-drop=ALL",
                  "--security-opt=no-new-privileges", "--user=65534:65534",
                  "--tmpfs=/tmp:rw,mode=1777", "--env=CHECKPOINT_DISABLE=1",
                  "--env=TF_IN_AUTOMATION=1"]
        try:
            if provider_directory is None:
                acquired = root / "acquired"
                acquired.mkdir(mode=0o700)
                # Keep acquisition files owned by this process for bounded cleanup.
                podman("run", "--rm", "--timeout=150", "--cidfile", str(cidfiles[0]),
                       "--userns=keep-id:uid=65534,gid=65534",
                       *common, "--volume", f"{source}:/config:ro",
                       "--volume", f"{acquired}:/provider-data:rw",
                       "--volume", f"{root / 'empty.tfrc'}:/empty.tfrc:ro",
                       "--env=TF_CLI_CONFIG_FILE=/empty.tfrc", "--env=TF_DATA_DIR=/provider-data",
                       image, "-chdir=/config", "init", "-backend=false", "-input=false",
                       "-lockfile=readonly", "-no-color", timeout=165)
                provider_directory = acquired / "providers"
            require(provider_directory.is_dir(), "provider directory")
            # Inputs and a fresh, never-enrolled key exist only in offline scratch.
            inputs = root / "inputs"
            inputs.mkdir(mode=0o755)
            inputs.chmod(0o755)
            for name in ("variables.tf", "generated_inputs.tf"):
                write(inputs / name, (source / name).read_bytes())
            cases = list(input_cases())
            for index, (values, _) in enumerate(cases):
                write(inputs / f"case-{index}.tfvars.json", json.dumps(values, ensure_ascii=True))
            write(source / "synthetic.tfvars.json", json.dumps(VALUES))
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            write(root / "synthetic.pem", key.private_bytes(serialization.Encoding.PEM,
                  serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
            digest = md5(key.public_key().public_bytes(serialization.Encoding.DER,
                         serialization.PublicFormat.SubjectPublicKeyInfo), usedforsecurity=False).hexdigest()
            fingerprint = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
            identity = podman("run", "--detach", "--timeout=480", "--cidfile", str(cidfiles[1]),
                             *common, "--network=none", "--volume", f"{root}:/check:ro",
                             "--volume", f"{provider_directory.resolve()}:/providers:ro",
                             "--env=TF_CLI_CONFIG_FILE=/check/empty.tfrc", "--env=TF_DATA_DIR=/tmp/provider-data",
                             "--env=OCI_AUTH=APIKey", "--env=OCI_REGION=us-ashburn-1",
                             "--env=OCI_TENANCY_OCID=ocid1.tenancy.oc1..synthetic",
                             "--env=OCI_USER_OCID=ocid1.user.oc1..synthetic",
                             "--env=OCI_FINGERPRINT=" + fingerprint,
                             "--env=OCI_PRIVATE_KEY_PATH=/check/synthetic.pem",
                             "--entrypoint=/bin/sleep", image, "480").strip()
            require(identity == cidfiles[1].read_text().strip()
                    and re.fullmatch(r"[a-f0-9]{64}", identity), "owned offline container")

            def terraform(*args, inputs_only=False, succeeds=True):
                data = "/tmp/input-data" if inputs_only else "/tmp/provider-data"
                path = "/check/inputs" if inputs_only else "/check/source"
                return podman("exec", "--env=TF_DATA_DIR=" + data, identity, "terraform",
                              "-chdir=" + path, *args, succeeds=succeeds)

            version = json.loads(terraform("version", "-json"))
            require(version["terraform_version"] == "1.5.7", "pinned Terraform CLI")
            terraform("init", "-backend=false", "-input=false", "-no-color", inputs_only=True)
            for index, (_, success) in enumerate(cases):
                diagnostic = terraform("plan", "-input=false", "-refresh=false", "-lock=false", "-no-color",
                                       f"-var-file=case-{index}.tfvars.json", inputs_only=True, succeeds=success)
                if not success:
                    require("automation" in diagnostic, "intended generated-input rejection")
            terraform("fmt", "-check", "-no-color")
            terraform("init", "-backend=false", "-input=false", "-lockfile=readonly",
                      "-plugin-dir=/providers", "-no-color")
            terraform("validate", "-no-color")
            graph = terraform("graph", "-type=plan", "-draw-cycles")
            terraform("graph", "-type=plan-destroy", "-draw-cycles")
            terraform("plan", "-input=false", "-refresh=false", "-lock=false", "-no-color",
                      "-var-file=synthetic.tfvars.json", "-out=/tmp/workload.plan")
            plan = json.loads(terraform("show", "-json", "/tmp/workload.plan"))
            expression = ('jsonencode(merge(local.host_metadata, {public_origin="https://synthetic.example.test",'
                          'data_volume_ocid="ocid1.volume.oc1.iad.synthetic",user_data=local.host_user_data}))\n')
            metadata = json.loads(json.loads(podman("exec", "--interactive", identity, "terraform",
                                                 "-chdir=/check/source", "console", "-no-color",
                                                 "-state=/tmp/synthetic-console.tfstate",
                                                 "-var-file=synthetic.tfvars.json", stdin=expression)))
            verify_plan(plan, graph, metadata)
            print(f"PASS: Terraform1.5.7/OCI9.8.0 {architecture}; {len(cases)} input cases, "
                  f"22-resource plan/graph, ZIP {package.sha256}; no OCI network or apply.")
        finally:
            cleanup(cidfiles)


def interrupted(_number, _frame):
    raise RuntimeError("Terraform checks interrupted or timed out")


if __name__ == "__main__":
    os.umask(0o077)  # Exercise restrictive caller permissions on every run.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-directory", type=Path, help="Existing locked provider tree; skip public download.")
    parser.add_argument("--pull-image", action="store_true", help="Fetch the pinned public native image first.")
    options = parser.parse_args()
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(number, interrupted)
    signal.alarm(660)
    try:
        verify(options.provider_directory, options.pull_image)
    finally:
        signal.alarm(0)
