/** ITAM 공용 상수 — 카테고리 한글 표기(목록·상세·QR라벨·실사·퇴사회수 공용). */
export const CAT_KO: Record<string, string> = {
  LAPTOP: "노트북", DESKTOP: "데스크톱", SERVER: "서버", NETWORK: "네트워크",
  PRINTER: "프린터", MOBILE: "모바일", MEDICAL_IT: "의료IT", PERIPHERAL: "주변기기" };

export function catKo(c: string): string {
  return CAT_KO[c] ?? c;
}
