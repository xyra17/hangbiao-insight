import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: ".",
  testMatch: "*.browser.ts",
  workers: 1,
  timeout: 60000,
  expect: { timeout: 7000 },
  outputDir: "./browser-results",
  reporter: [
    ["list"],
    [
      "json",
      {
        outputFile:
          "verification/frontend-polish-20261012/browser-results.json",
      },
    ],
  ],
  use: {
    baseURL: "http://127.0.0.1:5197",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    cwd: process.cwd(),
    command:
      "./node_modules/.bin/vite --config verification/frontend-polish-20261012/vite.controlled.ts",
    url: "http://127.0.0.1:5197",
    reuseExistingServer: false,
    timeout: 15000,
  },
});
