"""One process per mounted presenter store; host rotation restarts this process."""
import sys

import uvicorn

from .web import create_app


def main():
    try:
        app = create_app()
    except Exception:
        print('Application startup failed: check deployment configuration and storage.', file=sys.stderr)
        return 1
    uvicorn.run(app, host='0.0.0.0', port=8080, workers=1, proxy_headers=False,
                access_log=False, log_level='critical', server_header=False,
                limit_concurrency=16, backlog=32, timeout_keep_alive=5,
                timeout_graceful_shutdown=10)
    return 0


if __name__ == '__main__':
    sys.exit(main())
