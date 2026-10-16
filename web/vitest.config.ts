import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    coverage: {
      provider: "v8",
      include: ["src/**/*.ts"],
      // main.ts is DOM/WebSocket glue with no unit tests; it is checked by hand in the browser.
      exclude: ["src/**/*.test.ts", "src/main.ts"],
      thresholds: { statements: 90, branches: 80, functions: 85, lines: 90 },
    },
  },
});
