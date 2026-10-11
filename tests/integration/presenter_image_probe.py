"""Runs inside the built image via stdin; never copied into runtime layers."""
import http.client
from importlib import import_module
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
import platform
import sys


def inventory():
    assert os.getuid() == 10001 and os.getgid() == 10001, 'runtime user'
    assert platform.machine() == 'x86_64', 'runtime CPU'
    assert platform.python_version() == '3.12.15', 'runtime Python'
    assert os.path.ismount('/data'), 'actual data mount'
    assert {p.name for p in Path('/app').iterdir()} == {
        '.venv', 'LICENSE', 'frontend', 'troubleshooter_app'}, 'application allowlist'
    assert {p.name for p in Path('/app/frontend').iterdir()} == {'dist'}, 'frontend allowlist'
    assert not list(Path('/app/frontend/dist').rglob('*.map')), 'source maps'
    assert {p.name for p in Path('/app/troubleshooter_app').iterdir()} == {
        '__init__.py', '__main__.py', 'authentication.py', 'configuration.py',
        'credentials.py', 'sessions.py', 'web.py'}, 'source allowlist'
    assert 'Permission is hereby granted' in Path('/app/LICENSE').read_text(), 'project license'
    for name, expected in {'oci': '2.187.2', 'fastapi': '0.143.0',
                           'uvicorn': '0.54.0', 'argon2-cffi': '25.1.0'}.items():
        assert version(name) == expected, 'direct dependency version'
    for name in ('oci', 'fastapi', 'uvicorn', 'argon2', 'cryptography',
                 'pkcs11', 'crc32c', 'pydantic_core'):
        import_module(name)
    for name in ('oci', 'fastapi', 'uvicorn', 'argon2-cffi', 'argon2-cffi-bindings',
                 'cryptography', 'python-pkcs11', 'crc32c', 'pydantic-core'):
        package = distribution(name)
        notices = [p for p in package.files or ()
                   if '.dist-info/' in str(p) and 'license' in str(p).lower()]
        assert notices and all(package.locate_file(p).is_file() for p in notices), 'dependency notices'
    notices = Path('/app/frontend/dist/assets/third-party-notices.txt').read_text()
    for expected in ('react 19.3.0', 'react-dom 19.3.0', 'scheduler 0.28.0'):
        assert expected in notices, 'frontend notices'
    assert os.statvfs('/app').f_flag & os.ST_RDONLY, 'read-only root'
    assert os.statvfs('/run/demo').f_flag & os.ST_RDONLY, 'read-only configuration'
    return {'ok': True}


def request(spec):
    connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=3)
    try:
        connection.request(spec['method'], spec['path'],
                           body=spec.get('body'), headers=spec.get('headers', {}))
        response = connection.getresponse()
        body = response.read(512 * 1024 + 1)
        assert len(body) <= 512 * 1024, 'response size'
        return {'ok': True, 'status': response.status,
                'headers': dict(response.getheaders()), 'body': body.decode('utf-8')}
    finally:
        connection.close()


if __name__ == '__main__':
    try:
        spec = json.loads(sys.stdin.buffer.read(8192))
        result = inventory() if spec['mode'] == 'inventory' else request(spec)
    except AssertionError as error:
        result = {'ok': False, 'check': str(error)}
    except Exception:
        result = {'ok': False, 'check': 'probe unavailable'}
    print(json.dumps(result))
