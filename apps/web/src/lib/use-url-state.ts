"use client";
/** URL 쿼리 파라미터 ↔ 상태(X2-1) — 필터·선택 엔티티·탭을 URL에 보존해 새로고침 유지·공유 가능
 * ("이 케이스/이 날짜 봐주세요"를 URL로).
 *
 * useSearchParams로 반응형 읽기 + history.replaceState로 쓰기(네비게이션 없이). 문서상
 * pushState/replaceState는 Next Router와 통합돼 useSearchParams에 자동 반영된다. */
import { useCallback } from "react";
import { useSearchParams } from "next/navigation";

export function useUrlState(key: string, initial = ""): [string, (v: string) => void] {
  const params = useSearchParams();
  const val = params?.get(key) ?? initial;

  const set = useCallback((v: string) => {
    const p = new URLSearchParams(window.location.search);
    if (v) p.set(key, v); else p.delete(key);
    const qs = p.toString();
    window.history.replaceState(null, "",
      `${window.location.pathname}${qs ? "?" + qs : ""}`);
  }, [key]);

  return [val, set];
}
