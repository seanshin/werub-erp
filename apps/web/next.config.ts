import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone", // Docker 배포 (design §2.3)
  basePath: "/erp", // HIS 동거 — nginx /erp/ 프록시 하위 서빙 (SSO 쿠키 Path=/erp)
};

export default nextConfig;
