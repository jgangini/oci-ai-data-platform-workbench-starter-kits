import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const deployedSessionCookie = "__Host-aidp_lab_admin";
const developmentSessionCookie = "aidp_lab_admin_dev";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react()],
    server: {
      proxy: {
        "/api": {
          target: env.AIDP_API_PROXY_TARGET || "http://localhost:8000",
          changeOrigin: true,
          secure: false,
          configure: proxy => {
            // ponytail: Vite only bridges the secure production cookie to localhost HTTP; production keeps __Host- unchanged.
            proxy.on("proxyReq", (proxyRequest, request) => {
              const cookie = request.headers.cookie?.replace(developmentSessionCookie, deployedSessionCookie);
              if (cookie) proxyRequest.setHeader("cookie", cookie);
            });
            proxy.on("proxyRes", response => {
              const cookies = response.headers["set-cookie"];
              if (!cookies) return;
              response.headers["set-cookie"] = cookies.map(cookie => cookie.replace(deployedSessionCookie, developmentSessionCookie).replace(/;\s*Secure/gi, ""));
            });
          }
        }
      }
    }
  };
});
