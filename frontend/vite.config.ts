import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("/antd/") || id.includes("/@ant-design/") || id.includes("/rc-")) return "antd";
          if (id.includes("/echarts/") || id.includes("/echarts-for-react/")) return "charts";
          if (id.includes("/@tanstack/")) return "query";
          if (id.includes("/lucide-react/")) return "icons";
          if (id.includes("/react/") || id.includes("/react-dom/") || id.includes("/scheduler/")) return "react";
          return undefined;
        },
      },
    },
    // Ant Design is a shared, separately cached vendor chunk. Keep the warning
    // threshold above its measured size so the report focuses on application
    // chunks rather than a known framework bundle.
    chunkSizeWarningLimit: 900,
  },
  server: {
    // 5173 is occupied by the unrelated Globex workspace on this machine.
    port: 5184,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8280",
    },
  },
});
