/** 공통 표시 포맷 — 페이지별 중복 정의 제거(품질 백로그). */

/** 원화 — null/undefined는 0으로. 예: 12,000원 */
export const won = (n: number | null | undefined): string =>
  `${Number(n ?? 0).toLocaleString()}원`;

/** 원화(빈값은 대시) — null/undefined는 "-". */
export const wonOrDash = (n: number | null | undefined): string =>
  n == null ? "-" : `${Number(n).toLocaleString()}원`;

/** 만원 단위(반올림) — 표·요약에서 자릿수를 줄일 때. 예: 1,234만원 */
export const man = (n: number | null | undefined): string =>
  `${Math.round(Number(n ?? 0) / 10000).toLocaleString()}만원`;

/** 일시(로컬, ko-KR). */
export const dt = (s: string | null | undefined): string =>
  s ? new Date(s).toLocaleString("ko-KR") : "-";
