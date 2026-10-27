"use client";
/** 재무 — 고정자산 실사(재물조사): 개시(IN_USE 스냅샷)→라인 확인(정위치/소재불명/이전)→마감 집계.
 *  진행 중(OPEN) 실사는 동시 1건(중복 OPEN 차단). 마감 후 라인 불변. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryErrorBanner } from "@/components/query-guard";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

interface Inv { id: number; name: string; conducted_on: string; status: string }
interface Summary { total: number; found: number; not_found: number; relocated: number; not_checked: number }
interface Line {
  id: number; asset_id: number; expected_dept: number | null;
  found_status: string | null; actual_dept: number | null; note: string | null;
}
interface Detail { id: number; name: string; conducted_on: string; status: string; summary: Summary; lines: Line[] }

const FOUND_TONE: Record<string, "green" | "red" | "amber" | "gray"> = {
  FOUND: "green", NOT_FOUND: "red", RELOCATED: "amber", NOT_CHECKED: "gray",
};
const FOUND_LABEL: Record<string, string> = {
  FOUND: "정위치", NOT_FOUND: "소재불명", RELOCATED: "이전", NOT_CHECKED: "미확인",
};

export default function AssetInventoryPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [opening, setOpening] = useState(false);
  const [selId, setSelId] = useState<number | null>(null);
  const [discOnly, setDiscOnly] = useState(false);
  const [relocLine, setRelocLine] = useState<Line | null>(null);

  const list = useQuery({ queryKey: ["asset-invs"], queryFn: () =>
    api<{ items: Inv[] }>("/finance/asset-inventories") });
  const detail = useQuery({ enabled: selId != null, queryKey: ["asset-inv", selId, discOnly], queryFn: () =>
    api<Detail>(`/finance/asset-inventories/${selId}?discrepancy_only=${discOnly}`) });
  const refreshAll = () => {
    qc.invalidateQueries({ queryKey: ["asset-invs"] });
    qc.invalidateQueries({ queryKey: ["asset-inv"] });
  };

  const open = useMutation({
    mutationFn: (b: { name: string; conducted_on: string; note: string }) =>
      api<{ id: number }>("/finance/asset-inventories", { method: "POST", body: JSON.stringify(b) }),
    onSuccess: (r) => { toast.ok("실사 개시 — IN_USE 자산 스냅샷 생성"); setOpening(false);
      setSelId(r.id); refreshAll(); },
    onError: fail,
  });
  const check = useMutation({
    mutationFn: (v: { line_id: number; found_status: string; actual_dept: number | null }) =>
      api(`/finance/asset-inventory-lines/${v.line_id}/check`, { method: "POST",
        body: JSON.stringify({ found_status: v.found_status, actual_dept: v.actual_dept }) }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["asset-inv"] });
      setRelocLine(null); },   // v1.203 — 성공 시에만 닫기(실패 시 모달 유지·미확인 상태 놓침 방지)
    onError: fail,
  });
  const close = useMutation({
    mutationFn: (id: number) => api(`/finance/asset-inventories/${id}/close`, { method: "POST" }),
    onSuccess: () => { toast.ok("실사 마감 완료 — 결과 집계 확정"); refreshAll(); },
    onError: fail,
  });

  const onClose = async (id: number) => {
    if (await confirm({ title: "실사 마감", body: "마감 후에는 라인을 수정할 수 없습니다. 진행할까요?",
      danger: true, confirmLabel: "마감" }))
      close.mutate(id);
  };
  const setLine = (line: Line, status: string) => {
    if (status === "RELOCATED") { setRelocLine(line); return; }
    check.mutate({ line_id: line.id, found_status: status, actual_dept: null });
  };

  const d = detail.data;
  const isOpen = d?.status === "OPEN";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — 고정자산 실사</h1>
        <Button onClick={() => setOpening(true)}>실사 개시</Button>
      </div>
      <QueryErrorBanner queries={[list, detail]} />
      <p className="text-xs text-slate-500">
        재물조사 — 개시 시 사용 중(IN_USE) 자산을 라인으로 스냅샷하고, 라인별 정위치/소재불명/이전을
        확인한 뒤 마감합니다. 진행 중 실사는 동시 1건(중복 개시 차단), 마감 후 라인은 불변입니다.</p>

      <Card title="실사 목록">
        <Table headers={["ID", "명칭", "실시일", "상태", ""]}
          rows={(list.data?.items ?? []).map((iv) => [
            `#${iv.id}`, iv.name, iv.conducted_on,
            <Badge key="s" tone={iv.status === "OPEN" ? "green" : "gray"}>{iv.status}</Badge>,
            <Button key="v" variant="secondary" size="sm"
              onClick={() => { setSelId(iv.id); setDiscOnly(false); }}>열기</Button>,
          ])} empty={list.isFetching ? "불러오는 중…" : "실사 없음"} />
      </Card>

      {selId != null && d && (
        <Card title={`실사 #${d.id} — ${d.name} (${d.status})`} action={
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1 text-xs text-slate-500">
              <input type="checkbox" checked={discOnly}
                onChange={(e) => setDiscOnly(e.target.checked)} /> 불일치만</label>
            {isOpen && <Button size="sm" variant="danger" isLoading={close.isPending}
              onClick={() => onClose(d.id)}>마감</Button>}
            <Button size="sm" variant="secondary" onClick={() => setSelId(null)}>닫기</Button>
          </div>}>
          <div className="mb-3 flex flex-wrap gap-3 text-sm">
            <span>전체 <b>{d.summary.total}</b></span>
            <span className="text-emerald-700">정위치 {d.summary.found}</span>
            <span className="text-red-600">소재불명 {d.summary.not_found}</span>
            <span className="text-amber-700">이전 {d.summary.relocated}</span>
            <span className="text-slate-500">미확인 {d.summary.not_checked}</span>
          </div>
          <Table headers={["라인", "자산#", "기대부서", "상태", "실제부서", "확인"]}
            rows={d.lines.map((ln) => [
              `#${ln.id}`, `#${ln.asset_id}`, ln.expected_dept ?? "—",
              <Badge key="st" tone={FOUND_TONE[ln.found_status ?? "NOT_CHECKED"] ?? "gray"}>
                {FOUND_LABEL[ln.found_status ?? "NOT_CHECKED"]}</Badge>,
              ln.actual_dept ?? "—",
              isOpen ? (
                <span key="a" className="flex gap-1">
                  <Button variant="secondary" size="sm" onClick={() => setLine(ln, "FOUND")}>정위치</Button>
                  <Button variant="secondary" size="sm" onClick={() => setLine(ln, "NOT_FOUND")}>불명</Button>
                  <Button variant="secondary" size="sm" onClick={() => setLine(ln, "RELOCATED")}>이전</Button>
                </span>) : "—",
            ])} empty={detail.isFetching ? "불러오는 중…" : (discOnly ? "불일치 없음" : "라인 없음")} />
        </Card>
      )}

      {opening && (
        <Modal title="고정자산 실사 개시" onClose={() => setOpening(false)}>
          <OpenForm isLoading={open.isPending} onSubmit={(b) => open.mutate(b)} />
        </Modal>
      )}
      {relocLine && (
        <Modal title={`이전 확인 — 자산 #${relocLine.asset_id}`} onClose={() => setRelocLine(null)}>
          <RelocForm isLoading={check.isPending} onSubmit={(dept) => {
            check.mutate({ line_id: relocLine.id, found_status: "RELOCATED", actual_dept: dept });
          }} />
        </Modal>
      )}
    </div>
  );
}

function OpenForm({ onSubmit, isLoading }: {
  onSubmit: (b: { name: string; conducted_on: string; note: string }) => void; isLoading: boolean;
}) {
  const [name, setName] = useState("");
  const [conductedOn, setConductedOn] = useState(todayLocal());
  const [note, setNote] = useState("");
  return (
    <div className="space-y-2">
      <Input label="실사 명칭" value={name} onChange={(e) => setName(e.target.value)}
        placeholder="예: 2026 상반기 재물조사" />
      <Input label="실시일" type="date" value={conductedOn}
        onChange={(e) => setConductedOn(e.target.value)} />
      <Input label="비고(선택)" value={note} onChange={(e) => setNote(e.target.value)} />
      <p className="text-xs text-slate-500">개시 시 사용 중(IN_USE) 자산 전체가 라인으로 스냅샷됩니다.</p>
      <Button disabled={isLoading || !name} onClick={() => onSubmit({ name, conducted_on: conductedOn, note })}>
        개시</Button>
    </div>
  );
}

function RelocForm({ onSubmit, isLoading }: { onSubmit: (dept: number) => void; isLoading: boolean }) {
  const [dept, setDept] = useState("");
  return (
    <div className="space-y-2">
      <Input label="실제 발견 부서 ID" value={dept} inputMode="numeric"
        onChange={(e) => setDept(e.target.value.replace(/[^0-9]/g, ""))} />
      <p className="text-xs text-slate-500">타 부서에서 발견된 자산의 실제 위치 부서를 기록합니다.</p>
      <Button disabled={isLoading || !dept} onClick={() => onSubmit(Number(dept))}>이전 확인</Button>
    </div>
  );
}
