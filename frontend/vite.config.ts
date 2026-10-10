import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";
declare const process: { env: Record<string, string | undefined> };
export default defineConfig({
  plugins: [react(), tailwind()],
  server: {
    proxy: {
      "/api": process.env.AIRDROP_DEV_API_TARGET || "http://127.0.0.1:8000",
    },
  },
});
