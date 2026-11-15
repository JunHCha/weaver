import type { NextConfig } from "next";
const config: NextConfig = {
  ...(process.env.NODE_ENV === "development"
    ? {
        async rewrites() {
          return [
            {
              source: "/api/:path*",
              destination: "http://127.0.0.1:8765/api/:path*",
            },
          ];
        },
      }
    : { output: "export" }),
};
export default config;
