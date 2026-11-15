import { defineConfig } from "vitest/config";
export default defineConfig({
  test: {
    projects: [
      {
        esbuild: { jsx: "automatic" },
        test: {
          name: "unit",
          environment: "node",
          include: ["src/**/*.test.ts"],
        },
      },
      {
        esbuild: { jsx: "automatic" },
        test: {
          name: "components",
          environment: "jsdom",
          include: ["src/**/*.test.tsx"],
          setupFiles: ["./src/test/setup.ts"],
        },
      },
    ],
  },
});
