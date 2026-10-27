/** 로컬(KST) 날짜 유틸 — `new Date().toISOString()`은 UTC라 KST 00~09시에 "어제"로 밀리는
 * off-by-one을 유발(계산검토 FE-2 — 일마감·재고실사 일자 오입력). 항상 이 유틸을 쓸 것. */

/** Date → 로컬 기준 YYYY-MM-DD */
export function localISO(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** 오늘(로컬) YYYY-MM-DD */
export function todayLocal(): string {
  return localISO(new Date());
}

/** 당월(로컬) YYYY-MM */
export function monthLocal(): string {
  return todayLocal().slice(0, 7);
}

/** n일 전(로컬) YYYY-MM-DD */
export function daysAgoLocal(n: number): string {
  return localISO(new Date(Date.now() - n * 864e5));
}
