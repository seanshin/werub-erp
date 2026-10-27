"use client";
/** 재고 — 거래처(공급사) 관리(FR-14): 등록 → 등록심사 상태(PENDING/APPROVED/SUSPENDED/TERMINATED)
 *  → 월별 평가(납기·품질·단가)·스코어카드. APPROVED만 발주 가능(서버 게이트). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { monthLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const STATUS_TONE: Record<string, "gray" | "amber" | "green" | "red"> = {
  PENDING: "amber", APPROVED: "green", SUSPENDED: "red", TERMINATED: "gray",
};
const STATUS_LABEL: Record<string, string> = {
  PENDING: "심사중", APPROVED: "승인", SUSPENDED: "정지", TERMINATED: "해지",
};

interface Vendor {
  id: number; name: string; business_no: string | null; status: string;
  contact: string | null; is_active: boolean; vendor_type: string | null;
  eval_score: number | null;
}
interface Evaluation {
  period: string; delivery: number; quality: number; price: number;
  total: number; note: string | null;
}

export default function VendorsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [creating, setCreating] = useState(false);
  const [evaluating, setEvaluating] = useState<Vendor | null>(null);
  const [scoreOf, setScoreOf] = useState<Vendor | null>(null);

  // v1.203 — 쿼리키를 ["suppliers", …] 프리픽스로 통일(발주전환·위탁·반품 화면의 공급사
  // 드롭다운 캐시가 거래처 변경 후 30초간 낡은 값을 서빙하던 결함)
  const vendors = useQuery({ queryKey: ["suppliers", "all"], queryFn: () =>
    api<{ items: Vendor[] }>("/inventory/suppliers?active_only=false") });
  const scorecard = useQuery({ enabled: scoreOf != null, queryKey: ["vendor-score", scoreOf?.id],
    queryFn: () => api<{ evaluations: Evaluation[] }>(`/inventory/suppliers/${scoreOf!.id}/scorecard`) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["suppliers"] });

  const setStatus = useMutation({
    mutationFn: (v: { id: number; status: string }) =>
      api(`/inventory/suppliers/${v.id}/status`, { method: "PATCH",
        body: JSON.stringify({ status: v.status }) }),
    onSuccess: (_r, v) => { toast.ok(`상태 변경 — ${STATUS_LABEL[v.status] ?? v.status}`); refresh(); },
    onError: fail,
  });
  // v1.203 — 상태 select 즉시반영(해지 오클릭=발주 차단) 결함: confirm 경유로 변경
  const onStatusPick = async (v: Vendor, status: string) => {
    if (status === v.status) return;
    const danger = status === "SUSPENDED" || status === "TERMINATED";
    if (await confirm({
      title: "등록심사 상태 변경",
      body: `"${v.name}"을(를) ${STATUS_LABEL[v.status]} → ${STATUS_LABEL[status]}(으)로 변경할까요?${
        danger ? " 발주가 차단됩니다." : ""}`,
      danger, confirmLabel: "변경" })) setStatus.mutate({ id: v.id, status });
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재고 — 거래처(공급사) 관리</h1>
        <Button onClick={() => setCreating(true)}>거래처 등록</Button>
      </div>
      <p className="text-xs text-slate-500">
        공급사 등록·등록심사 상태·월별 평가(납기/품질/단가)를 관리합니다. <b>승인(APPROVED)</b> 상태만
        발주가 가능하며, 정지/해지 시 발주가 차단됩니다. 검진권 딜러 등 HIS 동기화 거래처도 함께 표시됩니다.</p>

      <QueryErrorBanner queries={[vendors]} />
      <Card title={`거래처 (${vendors.data?.items.length ?? 0})`}>
        <Table headers={["이름", "사업자번호", "연락처", "유형", "심사상태", "평가점수", "상태변경", ""]}
          rows={(vendors.data?.items ?? []).map((v) => [
            v.name, v.business_no ?? "—", v.contact ?? "—",
            v.vendor_type ?? "일반",
            <Badge key="s" tone={STATUS_TONE[v.status] ?? "gray"}>
              {STATUS_LABEL[v.status] ?? v.status}</Badge>,
            v.eval_score != null ? `${v.eval_score}점` : "—",
            <select key="st" value={v.status} disabled={setStatus.isPending}
              onChange={(e) => onStatusPick(v, e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1 text-xs">
              {Object.keys(STATUS_LABEL).map((s) =>
                <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
            </select>,
            <span key="a" className="flex gap-1">
              <Button variant="secondary" size="sm" onClick={() => setEvaluating(v)}>평가</Button>
              <Button variant="secondary" size="sm" onClick={() => setScoreOf(v)}>스코어카드</Button>
            </span>,
          ])} empty={vendors.isFetching ? "불러오는 중…" : "거래처 없음"} />
      </Card>

      {creating && (
        <Modal title="거래처 등록" onClose={() => setCreating(false)}>
          <CreateForm onDone={() => { setCreating(false); refresh(); }} onError={fail} />
        </Modal>
      )}
      {evaluating != null && (
        <Modal title={`${evaluating.name} — 월별 평가`} onClose={() => setEvaluating(null)}>
          <EvalForm vendor={evaluating}
            onDone={() => { setEvaluating(null); refresh(); }} onError={fail} />
        </Modal>
      )}
      {scoreOf != null && (
        <Modal title={`${scoreOf.name} — 평가 스코어카드`} size="lg" onClose={() => setScoreOf(null)}>
          <Table headers={["기간", "납기", "품질", "단가", "총점", "비고"]}
            rows={(scorecard.data?.evaluations ?? []).map((ev) => [
              ev.period, ev.delivery, ev.quality, ev.price, <b key="t">{ev.total}</b>,
              ev.note ?? "—",
            ])} empty={scorecard.isFetching ? "불러오는 중…" : "평가 이력 없음"} />
        </Modal>
      )}
    </div>
  );
}

function CreateForm({ onDone, onError }: { onDone: () => void; onError: (e: unknown) => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [businessNo, setBusinessNo] = useState("");
  const [contact, setContact] = useState("");
  const create = useMutation({
    mutationFn: () => api<{ id: number }>("/inventory/suppliers", { method: "POST",
      body: JSON.stringify({ name, business_no: businessNo, contact: contact || null }) }),
    onSuccess: () => { toast.ok("거래처 등록"); onDone(); },
    onError,
  });
  return (
    <div className="space-y-2">
      <Input label="이름" value={name} onChange={(e) => setName(e.target.value)}
        placeholder="예: 대한약품" />
      <Input label="사업자번호" value={businessNo} inputMode="numeric" maxLength={10}
        onChange={(e) => setBusinessNo(e.target.value.replace(/[^0-9]/g, ""))}
        placeholder="숫자만 10자리" />
      {businessNo !== "" && businessNo.length !== 10 && (
        <p className="text-xs text-amber-700">사업자번호는 10자리입니다.</p>)}
      <Input label="연락처(선택)" value={contact} onChange={(e) => setContact(e.target.value)} />
      <Button disabled={create.isPending || !name.trim() || businessNo.length !== 10}
        onClick={() => create.mutate()}>등록</Button>
    </div>
  );
}

function EvalForm({ vendor, onDone, onError }: {
  vendor: Vendor; onDone: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [period, setPeriod] = useState(monthLocal());
  const [delivery, setDelivery] = useState("");
  const [quality, setQuality] = useState("");
  const [price, setPrice] = useState("");
  const [note, setNote] = useState("");
  const evaluate = useMutation({
    mutationFn: () => api<{ total: number }>(`/inventory/suppliers/${vendor.id}/evaluate`, {
      method: "POST", body: JSON.stringify({ period, delivery: Number(delivery),
        quality: Number(quality), price: Number(price), note: note || null }) }),
    onSuccess: (r) => { toast.ok(`평가 등록 — 총점 ${r.total}점`); onDone(); },
    onError,
  });
  const valid = (s: string) => s !== "" && Number(s) >= 0 && Number(s) <= 100;
  const num = (setter: (v: string) => void) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setter(e.target.value.replace(/[^0-9.]/g, ""));
  return (
    <div className="space-y-2">
      <Input label="평가 기간(yyyy-mm)" value={period} onChange={(e) => setPeriod(e.target.value)} />
      <div className="grid grid-cols-3 gap-2">
        <Input label="납기준수(0~100)" value={delivery} inputMode="numeric" onChange={num(setDelivery)} />
        <Input label="검수품질(0~100)" value={quality} inputMode="numeric" onChange={num(setQuality)} />
        <Input label="단가경쟁(0~100)" value={price} inputMode="numeric" onChange={num(setPrice)} />
      </div>
      <Input label="비고(선택)" value={note} onChange={(e) => setNote(e.target.value)} />
      <Button disabled={evaluate.isPending || !/^\d{4}-\d{2}$/.test(period)
        || !valid(delivery) || !valid(quality) || !valid(price)}
        onClick={() => evaluate.mutate()}>평가 등록</Button>
    </div>
  );
}
