/** 404 — 알 수 없는 경로. 루트 레이아웃 내부 렌더. */
export default function NotFound() {
  return (
    <div style={{ padding: "3rem", textAlign: "center" }}>
      <h2 style={{ fontSize: "1.1rem", fontWeight: 700 }}>페이지를 찾을 수 없습니다 (404)</h2>
      <p style={{ marginTop: "0.5rem", fontSize: "0.9rem", color: "#475569" }}>
        요청하신 페이지가 존재하지 않거나 이동되었습니다.
      </p>
      <a href="/erp/dashboard"
        style={{ display: "inline-block", marginTop: "1rem", padding: "0.4rem 1rem",
                 border: "1px solid #cbd5e1", borderRadius: "0.375rem", color: "#0f172a",
                 textDecoration: "none" }}>
        대시보드로 이동
      </a>
    </div>
  );
}
