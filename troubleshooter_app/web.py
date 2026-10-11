"""Same-origin presenter HTTP boundary and bundled static application."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .authentication import Authentication, LoginBusy
from .configuration import RuntimeConfig, read_bounded
from .credentials import Verifier, password_valid, unique_fields
from .sessions import ABSOLUTE_SECONDS, SessionStore

COOKIE = "__Host-demo-session"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def error(code, status):
    return JSONResponse({"error": code}, status_code=status)


async def login_password(request):
    if (request.headers.get('content-type', '').lower() not in ('application/json', 'application/json; charset=utf-8')
            or request.headers.get('content-encoding')):
        raise ValueError()
    raw = bytearray()
    async with asyncio.timeout(5):
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise ValueError()
    value = json.loads(raw, object_pairs_hook=unique_fields)
    if type(value) is not dict or set(value) != {'password'} or not password_valid(value['password']):
        raise ValueError()
    return value['password']


def create_app(*, config_path=Path('/run/demo/config.json'),
               verifier_path=Path('/run/demo/presenter.json'), data_path=Path('/data'),
               assets_path=Path('/app/frontend/dist')):
    config = RuntimeConfig.load(config_path)
    verifier = Verifier.from_bytes(read_bounded(verifier_path, 1024))
    if not os.path.ismount(data_path) or not (assets_path / 'index.html').is_file():
        raise ValueError('application_storage_unavailable')
    store = SessionStore(data_path, verifier)
    authentication = Authentication(verifier, store)
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)

    @app.middleware('http')
    async def safe_response(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            response = error('service_unavailable', 503)
        response.headers.update(SECURITY_HEADERS)
        return response

    def same_origin(request):
        return request.headers.getlist('origin') == [config.public_origin]

    async def current_session(request):
        return await run_in_threadpool(store.lookup, request.cookies.get(COOKIE))

    @app.post('/api/login')
    async def login(request: Request):
        if not same_origin(request):
            return error('request_not_allowed', 403)
        try:
            password = await login_password(request)
        except (ValueError, UnicodeError, TimeoutError):
            return error('invalid_request', 400)
        try:
            session = await run_in_threadpool(authentication.login, password)
        except LoginBusy:
            response = error('login_limited', 429)
            response.headers['Retry-After'] = '60'
            return response
        if session is None:
            return error('invalid_credentials', 401)
        response = JSONResponse({'csrf': session.csrf})
        response.set_cookie(COOKIE, session.token, max_age=ABSOLUTE_SECONDS,
                            secure=True, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/api/session')
    async def session_info(request: Request):
        session = await current_session(request)
        return JSONResponse({'csrf': session.csrf}) if session else error('sign_in_required', 401)

    @app.post('/api/logout')
    async def logout(request: Request):
        if not same_origin(request):
            return error('request_not_allowed', 403)
        session = await current_session(request)
        if session is None:
            return error('sign_in_required', 401)
        tokens = request.headers.getlist('x-csrf-token')
        if (len(tokens) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', tokens[0])
                or not secrets.compare_digest(tokens[0], session.csrf)):
            return error('request_not_allowed', 403)
        await run_in_threadpool(store.revoke, session.token)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='strict', path='/')
        return response

    @app.get('/api/demo')
    async def demo_status(request: Request):
        if not await current_session(request):
            return error('sign_in_required', 401)
        return {'ready': False, 'message': 'Signed in. The incident demonstration is not yet available.'}

    @app.get('/healthz')
    async def health():
        await run_in_threadpool(store.check)
        return {'status': 'healthy'}

    @app.get('/')
    async def index():
        return FileResponse(assets_path / 'index.html')

    app.mount('/assets', StaticFiles(directory=assets_path / 'assets'), name='assets')
    return app
