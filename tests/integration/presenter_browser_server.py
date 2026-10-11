"""Local HTTPS fixture only; excluded from application images and default tests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from troubleshooter_app.credentials import make_verifier
from troubleshooter_app.web import create_app


def main():
    with tempfile.TemporaryDirectory(prefix='troubleshooter-browser-') as directory:
        root = Path(directory)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
                .sign(key, hashes.SHA256()))
        (root / 'tls.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root / 'tls.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
        (root / 'config.json').write_text(json.dumps({'public_origin': 'https://localhost:9443', 'release_sha': 'a' * 40}))
        (root / 'presenter.json').write_bytes(make_verifier('synthetic-browser-presenter-password').to_bytes())
        (root / 'data').mkdir()
        # Only this test fixture substitutes a temporary directory for a data mount.
        with patch('troubleshooter_app.web.os.path.ismount', return_value=True):
            app = create_app(config_path=root / 'config.json', verifier_path=root / 'presenter.json',
                             data_path=root / 'data', assets_path=ROOT / 'frontend/dist')
        uvicorn.run(app, host='127.0.0.1', port=9443, ssl_certfile=str(root / 'tls.pem'),
                    ssl_keyfile=str(root / 'tls.key'), proxy_headers=False, access_log=False,
                    log_level='critical', server_header=False)


if __name__ == '__main__':
    main()
