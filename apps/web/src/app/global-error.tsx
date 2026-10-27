"use client";
/** 루트 에러 바운더리 — 루트 레이아웃까지 실패 시(드묾). 자체 html/body 필요(루트 레이아웃 대체). */
export default function GlobalError({ reset, unstable_retry }: {
  error?: Error & { digest?: string };
  reset?: () => void;
  unstable_retry?: () => void;
}) {
  const retry = unstable_retry ?? reset;
  return (
    <html lang="ko">
      <body style={{ fontFamily: "Arial, sans-serif", padding: "3rem", textAlign: "center",
                     color: "#0f172a" }}>
        <h2 style={{ fontSize: "1.1rem", fontWeight: 700 }}>시스템 오류가 발생했습니다</h2>
        <p style={{ marginTop: "0.5rem", fontSize: "0.9rem", color: "#475569" }}>
          잠시 후 다시 시도해 주세요. 문제가 지속되면 관리자에게 문의하세요.
        </p>
        <button onClick={() => retry?.()}
          style={{ marginTop: "1rem", padding: "0.4rem 1rem", background: "#2563eb",
                   color: "#fff", border: "none", borderRadius: "0.375rem", cursor: "pointer" }}>
          다시 시도
        </button>
      </body>
    </html>
  );
}
