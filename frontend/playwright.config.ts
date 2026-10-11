import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  retries: 0,
  timeout: 30_000,
  reporter: 'list',
  use: { baseURL: 'https://localhost:9443', ignoreHTTPSErrors: true },
  webServer: {
    command: '../.venv/bin/python ../tests/integration/presenter_browser_server.py',
    url: 'https://localhost:9443/healthz',
    ignoreHTTPSErrors: true,
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
