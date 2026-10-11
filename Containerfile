FROM ghcr.io/astral-sh/uv@sha256:22fbda6ef3b6466750a6fb1c7ce1d23a2a63a53be91e03d408b6e4274753998d AS uv

FROM docker.io/library/node@sha256:51b1100cc2a83d370c6a60952e3f2989c8a43159d0e38586e090f3b3326efefd AS frontend
ENV npm_config_registry=https://registry.npmjs.org npm_config_cache=/tmp/npm-cache npm_config_fetch_timeout=30000 npm_config_fetch_retries=1 npm_config_update_notifier=false
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts frontend/playwright.config.ts ./
COPY frontend/src ./src
COPY frontend/tests ./tests
COPY container/frontend_notices.mjs /build/frontend_notices.mjs
RUN npm run typecheck && npm run build && node /build/frontend_notices.mjs

FROM docker.io/library/python@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv-cache UV_DEFAULT_INDEX=https://pypi.org/simple UV_HTTP_TIMEOUT=30 UV_HTTP_RETRIES=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-build --no-install-project --python /usr/local/bin/python

FROM docker.io/library/python@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
COPY --from=frontend /frontend/dist /app/frontend/dist
COPY troubleshooter_app /app/troubleshooter_app
COPY LICENSE /app/LICENSE
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "troubleshooter_app"]
