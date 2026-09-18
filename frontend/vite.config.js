import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Relative asset URLs, so one build works under any URL prefix. The backend
  // injects <base href="..."> at serve time and the browser resolves against it.
  base: "./",
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
  },
});
