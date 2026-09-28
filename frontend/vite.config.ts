import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // 5173 is occupied by the unrelated Globex workspace on this machine.
    port: 5184,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8280",
    },
  },
});
