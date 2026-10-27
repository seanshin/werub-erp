"use client";
/** id→이름 조회맵(X2-6) — 화면에 노출되던 숫자 ID(부서·직원 등)를 이름으로 표시.
 * 목록 EP를 캐시(5분)해 id→name Map을 만들고 resolver 반환. 미조회 id는 #id 폴백. */
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

export function useNameMap(key: string, endpoint: string,
                          opts?: { idField?: string; nameField?: string }) {
  const idF = opts?.idField ?? "id";
  const nameF = opts?.nameField ?? "name";
  const q = useQuery({
    queryKey: ["name-map", key],
    queryFn: () => api<{ items: Record<string, unknown>[] }>(endpoint),
    staleTime: 300_000,
    retry: 0,
  });
  const map = new Map<number, string>();
  for (const it of q.data?.items ?? []) map.set(Number(it[idF]), String(it[nameF] ?? ""));
  return (id: number | null | undefined): string =>
    id == null ? "-" : (map.get(id) || `#${id}`);
}
