import {defineConfig} from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  timeout: 45_000,
  use: {
    baseURL: process.env.EBOOK_TEST_URL || 'http://localhost:18080',
    channel: process.env.EBOOK_TEST_BROWSER || undefined,
  },
});

