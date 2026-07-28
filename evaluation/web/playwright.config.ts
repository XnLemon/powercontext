import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.e2e.spec.ts",
  outputDir: "./node_modules/.cache/playwright",
});
