"use client";
/** 계정과목 콤보박스(X2-5) — 코드 raw 텍스트 입력(placeholder "1110") 대체. 코드→명 에코. */
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { Combobox } from "@/components/combobox";

interface Acct { id: number; code: string; name: string; kind_kr: string; active: boolean }

export function AccountPicker({ value, onPick, width = "w-full" }: {
  value: string; onPick: (code: string) => void; width?: string }) {
  const q = useQuery({ queryKey: ["fin-accounts"],
    queryFn: () => api<{ items: Acct[] }>("/finance/accounts?include_inactive=false") });
  const opts = q.data?.items ?? [];
  const sel = opts.find((a) => a.code === value) ?? (value ? { code: value } as Acct : null);
  return (
    <Combobox<Acct>
      value={sel}
      onPick={(a) => onPick(a?.code ?? "")}
      options={opts}
      getKey={(a) => a.code}
      getLabel={(a) => (a.name ? `${a.code} ${a.name}` : a.code)}
      getSearch={(a) => `${a.code} ${a.name ?? ""} ${a.kind_kr ?? ""}`}
      placeholder="계정 코드·명 검색"
      width={width} />
  );
}
