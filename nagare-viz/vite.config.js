import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileServerPlugin } from "./src/api/fileServerPlugin.ts";
export default defineConfig({
    plugins: [react(), fileServerPlugin()],
    server: {
        port: 5380,
        host: "127.0.0.1",
        strictPort: true,
        open: true,
    },
});
