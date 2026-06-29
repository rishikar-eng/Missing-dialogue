import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base: "./" -> relative asset paths so Electron can load dist/index.html via file://
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: { port: 5173, strictPort: true },
  build: { outDir: "dist", emptyOutDir: true },
});
