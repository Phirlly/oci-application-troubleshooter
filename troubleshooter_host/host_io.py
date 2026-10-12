"""Bounded host process, metadata and durable-file boundaries."""
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from .inputs import HostError, HostInputs, unique_fields


def atomic_write(path, content, *, mode=0o600, gid=None):
    path = Path(path)
    temporary = None
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError()
        descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        with os.fdopen(descriptor, "wb") as output:
            if gid is not None:
                os.fchown(output.fileno(), os.geteuid(), gid)
            os.fchmod(output.fileno(), mode)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, ValueError):
        raise HostError("host_file_unavailable") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, maximum=16384):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError()
            raw = source.read(maximum + 1)
        if len(raw) > maximum:
            raise ValueError()
        return json.loads(raw, object_pairs_hook=unique_fields)
    except (OSError, ValueError, UnicodeError):
        raise HostError("host_file_unavailable") from None


def command(arguments, *, timeout=30, maximum=262144, stdin=b"", file_limit=None):
    """No inherited secrets, shell, unbounded output or surviving timed-out group."""
    environment = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                   "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/root",
                   "OCI_HEADER_PARSING_ERROR_MAX_RETRIES": "0"}
    process = None
    try:
        with tempfile.TemporaryFile() as input_file, selectors.DefaultSelector() as selector:
            input_file.write(stdin)
            input_file.seek(0)

            def limit_file():
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))

            process = subprocess.Popen(arguments, stdin=input_file, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=environment, start_new_session=True,
                                       preexec_fn=limit_file if file_limit is not None else None)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            deadline, total, output = time.monotonic() + timeout, 0, bytearray()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError()
                for key, _ in selector.select(min(remaining, 0.25)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > maximum:
                        raise ValueError()
                    if key.fileobj is process.stdout:
                        output.extend(chunk)
            if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
                raise ValueError()
            return bytes(output)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise HostError("host_command_failed") from None
    finally:
        if process is not None:
            # Also catches descendants retaining stdout after the parent exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def load_metadata():
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        request = Request("http://169.254.169.254/opc/v2/instance/",
                          headers={"Authorization": "Bearer Oracle", "Accept": "application/json"})
        with opener.open(request, timeout=5) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        return HostInputs.from_imds(json.loads(raw, object_pairs_hook=unique_fields))
    except (OSError, ValueError, UnicodeError):
        raise HostError("host_metadata_unavailable") from None
