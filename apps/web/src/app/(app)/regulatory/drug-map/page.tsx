"use client";
/** 규제 — 약품 코드 매핑. HIS 비표준코드(M*) ↔ 보험 제품코드 자동 후보·약제팀 확정. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";

interface Cand { code: string; name: string; unit_price: number }
interface DMap { his_code: string; insurance_code: string | null; drug_name: string | null;
  ingredient: string | null; status: string; confidence: string | null;
  candidates: Cand[] | null; matched_name: string | null }

const CONF: Record<string, { label: string; tone: "green" | "amber" | "red" | "gray" }> = {
  HIGH: { label: "높음", tone: "green" }, MEDIUM: { label: "후보다수", tone: "amber" },
  LOW: { label: "성분매칭", tone: "red" } };
const STAT: Record<string, { label: string; tone: "amber" | "green" | "gray" }> = {
  SUGGESTED: { label: "검토대기", tone: "amber" }, CONFIRMED: { label: "확정", tone: "green" },
  REJECTED: { label: "제외", tone: "gray" } };

// 함량(용량) 추출·정규화 — 약제팀 확정 안전(오매칭 함량 불일치 경고)
const _normUnit = (u: string) => {
  const x = u.toLowerCase();
  if (/밀리그[램람]|mg/.test(x)) return "mg";
  if (/마이크로그램|mcg|㎍/.test(x)) return "mcg";
  if (/단위|iu/.test(x)) return "iu";
  if (/밀리리터|ml/.test(x)) return "ml";
  return x;
};
const strengthOf = (s: string | null): string | null => {
  if (!s) return null;
  const m = s.match(/(\d[\d,.]*)\s*(밀리그램|밀리그람|마이크로그램|mcg|㎍|mg|iu|단위|ml|밀리리터|g)/i);
  return m ? `${m[1].replace(/[,]/g, "")}${_normUnit(m[2])}` : null;
};
// HIS 약품명과 후보명의 함량이 둘 다 있고 다르면 불일치(오매칭 의심)
const strengthMismatch = (his: string | null, cand: string): boolean => {
  const a = strengthOf(his), b = strengthOf(cand);
  return !!(a && b && a !== b);
};

export default function DrugMapPage() {
  const qc = useQueryClient();
  const ask = useConfirm();
  const [status, setStatus] = useState("SUGGESTED");
  const [page, setPage] = useState(1);
  const toast = useToast();
  const SIZE = 50;
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const refresh = () => qc.invalidateQueries({ queryKey: ["drug-map"] });
  const selectStatus = (st: string) => { setStatus(st); setPage(1); };  // 상태 전환 시 1페이지로

  const maps = useQuery({ queryKey: ["drug-map", status, page],
    queryFn: () => api<{ count: number; items: DMap[]; page: number; size: number }>(
      `/admin/regulatory/drug-map?page=${page}&size=${SIZE}${status ? `&status=${status}` : ""}`) });
  const total = maps.data?.count ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / SIZE));
  const build = useMutation({
    mutationFn: () => api<{ created: number }>("/admin/regulatory/drug-map/build", { method: "POST" }),
    onSuccess: (r) => { toast.ok(`자동 후보 ${r.created}건 빌드`); refresh(); }, onError: fail });
  const confirm = useMutation({
    mutationFn: (p: { his_code: string; insurance_code: string }) =>
      api("/admin/regulatory/drug-map/confirm", { method: "POST", body: JSON.stringify(p) }),
    onSuccess: () => { toast.ok("보험코드 확정 — 이후 라인 자동 변환"); refresh(); }, onError: fail });
  const reject = useMutation({
    mutationFn: (his_code: string) =>
      api(`/admin/regulatory/drug-map/${his_code}/reject`, { method: "POST" }),
    onSuccess: () => refresh(), onError: fail });


  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">규제 — 약품 코드 매핑 (HIS 비표준코드 ↔ 보험코드)</h1>
      <p className="text-xs text-slate-400">
        HIS가 비표준 약품코드(M*)로 청구 → 명칭·성분 자동 매칭 후보 → 약제팀 확정 → 이후 라인 자동 변환(보험코드 단가).
      </p>

      <Card>
        <div className="mb-3 flex items-center gap-2">
          {["SUGGESTED", "CONFIRMED", "REJECTED", ""].map((st) => (
            <button key={st || "ALL"} onClick={() => selectStatus(st)}
              className={`rounded-md px-3 py-1.5 text-sm font-medium ${status === st
                ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
              {st ? (STAT[st]?.label ?? st) : "전체"}</button>
          ))}
          <span className="ml-auto text-xs text-slate-400">{status === "SUGGESTED" ? "검토대기 " : ""}{maps.data?.count ?? 0}건</span>
          <button onClick={() => build.mutate()} disabled={build.isPending}
            className="rounded-md bg-violet-600 px-4 py-1.5 text-sm font-medium text-white disabled:opacity-50">
            미등재 약품 자동 후보 빌드</button>
        </div>

        <Table sortable pageSize={30}
          headers={["HIS코드", "약품명", "성분", "신뢰도", "보험코드 후보(클릭=확정)", "상태"]}
          empty={maps.isFetching ? "불러오는 중…" : "대상 없음 — 자동 후보 빌드를 실행하세요"}
          rows={(maps.data?.items ?? []).map((m) => [
            <code key="h" className="text-xs">{m.his_code}</code>,
            <span key="n" className="text-sm">{m.drug_name ?? "-"}</span>,
            <span key="i" className="text-xs text-slate-500">{m.ingredient ?? "-"}</span>,
            m.confidence ? <Badge key="c" tone={CONF[m.confidence]?.tone ?? "gray"}>{CONF[m.confidence]?.label}</Badge>
              : <span key="c" className="text-slate-400">미매칭</span>,
            <div key="cand" className="space-y-0.5">
              {m.status === "CONFIRMED"
                ? <span className="text-xs"><b>{m.insurance_code}</b> · {m.matched_name}</span>
                : (m.candidates ?? []).map((c) => {
                    const bad = strengthMismatch(m.drug_name, c.name);
                    return (
                      <button key={c.code}
                        onClick={async () => { if (!bad || await ask({
                          title: "함량 불일치 의심", danger: true, confirmLabel: "확정",
                          body: `HIS "${m.drug_name}" ↔ 후보 "${c.name}" — 함량이 일치하지 않습니다. 그래도 확정하시겠습니까?` }))
                          confirm.mutate({ his_code: m.his_code, insurance_code: c.code }); }}
                        className={`block w-full rounded px-1.5 py-0.5 text-left text-[11px] ${bad
                          ? "bg-rose-50 text-rose-700 hover:bg-rose-100"
                          : "bg-emerald-50 text-emerald-700 hover:bg-emerald-100"}`}>
                        {bad && "⚠ 함량불일치 "}{c.code} · {won(c.unit_price)} · {c.name}</button>);
                  })}
              {m.status !== "CONFIRMED" && (m.candidates ?? []).length === 0 &&
                <span className="text-xs text-slate-400">후보 없음 — HIS 보험코드 직접 회신 필요</span>}
            </div>,
            <span key="s" className="flex items-center gap-1">
              <Badge tone={STAT[m.status]?.tone ?? "gray"}>{STAT[m.status]?.label ?? m.status}</Badge>
              {m.status === "SUGGESTED" && (
                <button onClick={() => reject.mutate(m.his_code)}
                  className="rounded bg-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600">제외</button>
              )}
            </span>,
          ])} />
        {total > SIZE && (
          <div className="mt-3 flex items-center justify-center gap-3 text-sm">
            <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}
              className="rounded-md border border-slate-200 px-3 py-1 disabled:opacity-40 hover:bg-slate-50">이전</button>
            <span className="text-slate-500">{page} / {lastPage}</span>
            <button onClick={() => setPage((p) => Math.min(lastPage, p + 1))} disabled={page >= lastPage}
              className="rounded-md border border-slate-200 px-3 py-1 disabled:opacity-40 hover:bg-slate-50">다음</button>
          </div>
        )}
      </Card>
    </div>
  );
}
