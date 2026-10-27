import type { Metadata, Viewport } from "next";
import "./globals.css";
import Providers from "./providers";

// 격리망 오프라인 빌드 — 외부 폰트(next/font/google) 미사용, 시스템 폰트 스택 (design §6.7)
export const metadata: Metadata = {
  title: "병원 ERP",
  description: "병원용 ERP + 인사관리 시스템",
};

// 모바일 대응 — 폰/태블릿(재고 워크스테이션 현장) 반응형 스케일링
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko" className="h-full antialiased">
      <body className="min-h-full bg-slate-50 text-slate-900">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
