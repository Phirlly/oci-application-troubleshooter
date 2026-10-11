"""Opt-in real Podman image check; synthetic data, no published ports or cloud."""
import argparse
from http.cookies import SimpleCookie
import json
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from troubleshooter_app.credentials import make_verifier

PROBE = Path(__file__).with_name('presenter_image_probe.py').read_text()
ORIGIN = 'https://demo.example.test'
PASSWORD = 'synthetic-image-presenter-password'
STARTUP_ERROR = 'Application startup failed: check deployment configuration and storage.'


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def podman(*arguments, stdin=None, timeout=30, success=True):
    result = subprocess.run(['podman', *arguments], input=stdin, text=True,
                            capture_output=True, timeout=timeout, check=False)
    require(not success or result.returncode == 0, 'Podman ' + arguments[0])
    return result


def probe(container, spec):
    output = podman('exec', '-i', container, 'python', '-c', PROBE,
                    stdin=json.dumps(spec), timeout=10).stdout
    require(len(output) <= 600_000, 'probe response size')
    return json.loads(output)


def request(container, path, *, method='GET', headers=None, body=None):
    result = probe(container, {'mode': 'request', 'path': path, 'method': method,
                              'headers': headers or {}, 'body': body})
    require(result['ok'], 'HTTP probe')
    return result


def ready(container):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = probe(container, {'mode': 'request', 'path': '/healthz', 'method': 'GET'})
        if result['ok'] and result['status'] == 200:
            return
        time.sleep(0.25)
    raise RuntimeError('startup deadline')


def login(container, password):
    result = request(container, '/api/login', method='POST',
                     headers={'Origin': ORIGIN, 'Content-Type': 'application/json'},
                     body=json.dumps({'password': password}))
    require(result['status'] == 200, 'login')
    headers = {key.lower(): value for key, value in result['headers'].items()}
    cookie = SimpleCookie(headers['set-cookie'])
    require(set(cookie) == {'__Host-demo-session'}, 'session cookie name')
    value = cookie['__Host-demo-session']
    require(value['secure'] and value['httponly'] and value['path'] == '/'
            and value['samesite'].lower() == 'strict' and not value['domain'], 'cookie protection')
    return {'Cookie': '__Host-demo-session=' + value.value}, json.loads(result['body'])['csrf']


def cleanup_owned(cidfiles):
    # Finish each bounded removal before delivering cancellation again.
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM, signal.SIGTERM, signal.SIGINT})
    signal.alarm(0)
    try:
        failures = []
        for cidfile in cidfiles:
            try:
                if not cidfile.exists():
                    continue
                container = cidfile.read_text().strip()
                if not re.fullmatch(r'[a-f0-9]{64}', container):
                    failures.append('invalid created container identity')
                    continue
                result = podman('rm', '--force', '--ignore', container, success=False)
                if result.returncode:
                    failures.append('container removal')
            except (OSError, subprocess.TimeoutExpired):
                failures.append('container removal')
        require(not failures, 'owned container cleanup')
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def verify(image):
    details = json.loads(podman('image', 'inspect', image).stdout)[0]
    require(details['Architecture'] == 'amd64' and details['Os'] == 'linux', 'image platform')
    require(details['Config']['User'] == '10001:10001', 'image user')
    require(details['Config']['Entrypoint'] == ['python', '-m', 'troubleshooter_app'], 'image entrypoint')
    with tempfile.TemporaryDirectory(prefix='presenter-image-') as directory:
        root = Path(directory)
        runtime, data = root / 'runtime', root / 'data'
        runtime.mkdir(mode=0o700)
        data.mkdir(mode=0o700)
        (runtime / 'config.json').write_text(json.dumps({'public_origin': ORIGIN, 'release_sha': 'a' * 40}))
        (runtime / 'presenter.json').write_bytes(make_verifier(PASSWORD).to_bytes())
        for path in runtime.iterdir():
            path.chmod(0o600)
        common = ['--pull=never', '--read-only', '--read-only-tmpfs=false', '--network=none',
                  '--cap-drop=ALL', '--security-opt=no-new-privileges',
                  '--userns=keep-id:uid=10001,gid=10001', '--volume', f'{runtime}:/run/demo:ro']
        cidfiles = [root / 'missing.cid', root / 'running.cid']
        try:
            missing = podman('run', '--rm', '--timeout', '45', '--cidfile', str(cidfiles[0]),
                             *common, image, timeout=60, success=False)
            require(missing.returncode == 1 and STARTUP_ERROR in missing.stderr
                    and 'Traceback' not in missing.stderr and not missing.stdout, 'missing mount rejection')
            podman('run', '--detach', '--timeout', '180', '--cidfile', str(cidfiles[1]),
                   *common, '--volume', f'{data}:/data:rw', image)
            container = cidfiles[1].read_text().strip()
            require(re.fullmatch(r'[a-f0-9]{64}', container), 'created container identity')
            ready(container)
            result = probe(container, {'mode': 'inventory'})
            require(result['ok'], 'image inventory: ' + result.get('check', 'unknown'))
            require(request(container, '/api/demo')['status'] == 401, 'unauthenticated denial')
            page = request(container, '/')
            require(page['status'] == 200, 'bundled page')
            assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', page['body'])
            require(len(assets) >= 2, 'bundled asset references')
            for asset in [*assets, '/assets/third-party-notices.txt']:
                result = request(container, asset)
                require(result['status'] == 200 and result['body'], 'served asset')
            denied = request(container, '/api/login', method='POST', headers={'Origin': 'https://wrong.example.test'})
            require(denied['status'] == 403, 'wrong origin denial')
            cookie, _ = login(container, PASSWORD)
            status = request(container, '/api/demo', headers=cookie)
            require(status['status'] == 200 and json.loads(status['body'])['ready'] is False, 'honest demo status')
            podman('stop', '--time', '10', container)
            podman('start', container)
            ready(container)
            require(request(container, '/api/session', headers=cookie)['status'] == 200, 'restart preserves session')
            podman('stop', '--time', '10', container)
            replacement = runtime / 'presenter.pending'
            replacement.write_bytes(make_verifier(PASSWORD + '-rotated').to_bytes())
            replacement.chmod(0o600)
            replacement.replace(runtime / 'presenter.json')
            podman('start', container)
            ready(container)
            require(request(container, '/api/session', headers=cookie)['status'] == 401, 'rotation revokes session')
            cookie, csrf = login(container, PASSWORD + '-rotated')
            result = request(container, '/api/logout', method='POST',
                             headers=cookie | {'Origin': ORIGIN, 'X-CSRF-Token': csrf})
            require(result['status'] == 204, 'logout')
            require(request(container, '/api/session', headers=cookie)['status'] == 401, 'logout revokes session')
            require((data / 'presenter.sqlite3').stat().st_mode & 0o777 == 0o600, 'database permissions')
        finally:
            cleanup_owned(cidfiles)


def interrupted(_signum, _frame):
    raise RuntimeError('image verification interrupted or timed out')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image-id-file', required=True, type=Path)
    options = parser.parse_args()
    image = options.image_id_file.read_text().strip()
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', image), 'built image identity')
    for number in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
        signal.signal(number, interrupted)
    signal.alarm(240)
    try:
        verify(image)
    finally:
        signal.alarm(0)
    print('Image startup, assets, isolation, session restart, rotation and cleanup passed.')


if __name__ == '__main__':
    main()
