import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The api the dev server forwards to: a stack started with `make up` publishes it here.
const api = `http://127.0.0.1:${process.env["VISIONQUERY_API_PORT"] ?? "8080"}`;

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/health": api },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    unstubGlobals: true,
    restoreMocks: true,
  },
});
