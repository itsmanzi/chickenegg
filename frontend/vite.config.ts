import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.VITE_API_URL || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/analyze": { target: api, changeOrigin: true },
      "/health": { target: api, changeOrigin: true },
      "/collect-email": { target: api, changeOrigin: true },
    },
  },
});
