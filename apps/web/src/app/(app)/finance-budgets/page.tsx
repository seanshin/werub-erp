"use client";
/** 재무 — 예산 관리(FR-09): 편성·증액/감액·전용·이월·약정. 3-way 통제(예산 vs 약정 vs 집행 = 가용).
 *  편성/변경(write)=DIRECTOR/ADMIN. 집행률·이력 조회. 부서장은 본부서(서버 scope). */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

interface ExecRow {
  department_id: number; account_code: string; budget: number; spent: number;
  committed: number; available: number; variance: number; rate: number;
  used_rate: number; status: string;
}
interface Amendment {
  department_id: number; account_code: string; old_amount: number | null;
  new_amount: number; reason: string | null; changed_at: string;
}
interface Enc {
  id: number; department_id: number; account_code: string; amount: number;
  description: string | null; source_ref: string | null; status: string; created_at: string | null;
}
interface Dept { id: number; name: string; code: string }
interface Acct { code: string; name: string }

const THIS_YEAR = 2026;
const statusTone = (s: string) => s === "OVER" ? "red" : s === "WARNING" ? "amber" : "green";

export default function BudgetsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [year, setYear] = useState(THIS_YEAR);
  const [modal, setModal] = useState<null | "amend" | "transfer" | "carry" | "encumber">(null);

  const exec = useQuery({ queryKey: ["budget-exec", year], queryFn: () =>
    api<{ items: ExecRow[] }>(`/finance/budgets/execution?fiscal_year=${year}`) });
  const amend = useQuery({ queryKey: ["budget-amend", year], queryFn: () =>
    api<{ items: Amendment[] }>(`/finance/budgets/amendments?fiscal_year=${year}`) });
  const encs = useQuery({ queryKey: ["budget-enc", year], queryFn: () =>
    api<{ items: Enc[] }>(`/finance/budgets/encumbrances?fiscal_year=${year}&status=OPEN`) });
  const depts = useQuery({ queryKey: ["departments"], queryFn: () =>
    api<{ items: Dept[] }>("/departments") });
  const accts = useQuery({ queryKey: ["accounts-all"], queryFn: () =>
    api<{ items: Acct[] }>("/finance/accounts") });

  const deptName = useMemo(() => Object.fromEntries(
    (depts.data?.items ?? []).map((d) => [d.id, d.name])), [depts.data]);
  const acctName = useMemo(() => Object.fromEntries(
    (accts.data?.items ?? []).map((a) => [a.code, a.name])), [accts.data]);

  const refresh = () => { for (const k of ["budget-exec", "budget-amend", "budget-enc"])
    qc.invalidateQueries({ queryKey: [k, year] }); };

  const release = useMutation({
    mutationFn: (id: number) => api(`/finance/budgets/encumbrances/${id}/release`, { method: "POST" }),
    onSuccess: () => { toast.ok("약정 해제 (가용예산 복원)"); refresh(); }, onError: fail });

  const onRelease = async (e: Enc) => {
    if (await confirm({ title: "약정 해제", confirmLabel: "해제",
      body: `${deptName[e.department_id] ?? e.department_id} · ${e.account_code} 약정 ${won(e.amount)}을(를) 해제합니다. 가용예산이 복원됩니다.` }))
      release.mutate(e.id);
  };

  const totals = (exec.data?.items ?? []).reduce((a, r) => ({
    budget: a.budget + r.budget, spent: a.spent + r.spent,
    committed: a.committed + r.committed, available: a.available + r.available,
  }), { budget: 0, spent: 0, committed: 0, available: 0 });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">재무 — 예산 관리</h1>
        <div className="flex items-center gap-2">
          <label className="text-sm text-slate-600">회계연도
            <input type="number" value={year} onChange={(e) => setYear(Number(e.target.value))}
              className="ml-1 w-24 rounded-md border border-slate-300 px-2 py-1 text-sm" />
          </label>
          <Button size="sm" onClick={() => setModal("amend")}>편성·증액/감액</Button>
          <Button size="sm" variant="secondary" onClick={() => setModal("transfer")}>전용</Button>
          <Button size="sm" variant="secondary" onClick={() => setModal("carry")}>이월</Button>
          <Button size="sm" variant="secondary" onClick={() => setModal("encumber")}>약정 등록</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        3-way 통제 — 가용 = 예산 − 집행(전표) − 약정(지출예정). 편성·전용·이월은 관리자,
        부서장은 본부서 조회. 초과(OVER)·경고(90%+)를 색으로 표시합니다.</p>

      <QueryErrorBanner queries={[exec, amend, encs]} />

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        {([["예산", totals.budget], ["집행", totals.spent], ["약정", totals.committed],
           ["가용", totals.available]] as const).map(([k, v]) => (
          <div key={k} className="rounded-lg border border-slate-200 p-2">
            <p className="text-xs text-slate-500">{k}</p>
            <p className={`text-sm font-bold ${k === "가용" && v < 0 ? "text-red-600" : ""}`}>{won(v)}</p>
          </div>))}
      </div>

      <Card title={`집행률 (${year})`}>
        <Table headers={["부서", "계정", "예산", "집행", "약정", "가용", "소진율", "상태"]}
          rows={(exec.data?.items ?? []).map((r) => [
            deptName[r.department_id] ?? `#${r.department_id}`,
            <span key="a"><code className="text-xs">{r.account_code}</code>
              <span className="ml-1 text-xs text-slate-400">{acctName[r.account_code] ?? ""}</span></span>,
            won(r.budget), won(r.spent), won(r.committed),
            <span key="v" className={r.available < 0 ? "font-semibold text-red-600" : ""}>{won(r.available)}</span>,
            `${r.used_rate}%`,
            <Badge key="s" tone={statusTone(r.status)}>{r.status}</Badge>,
          ])} empty={exec.isFetching ? "불러오는 중…" : "편성된 예산 없음"} />
      </Card>

      <Card title={`약정(OPEN) — ${encs.data?.items?.length ?? 0}건`}>
        <Table headers={["부서", "계정", "금액", "내역", "참조", ""]}
          rows={(encs.data?.items ?? []).map((e) => [
            deptName[e.department_id] ?? `#${e.department_id}`, e.account_code, won(e.amount),
            e.description ?? "—", e.source_ref ?? "—",
            <Button key="r" size="sm" variant="secondary" disabled={release.isPending}
              onClick={() => onRelease(e)}>해제</Button>,
          ])} empty={encs.isFetching ? "불러오는 중…" : "OPEN 약정 없음"} />
      </Card>

      <Card title="예산 변경 이력">
        <Table headers={["부서", "계정", "이전", "변경", "사유", "일시"]}
          rows={(amend.data?.items ?? []).map((a) => [
            deptName[a.department_id] ?? `#${a.department_id}`, a.account_code,
            a.old_amount != null ? won(a.old_amount) : "—", won(a.new_amount),
            a.reason || "—", a.changed_at.slice(0, 10),
          ])} empty={amend.isFetching ? "불러오는 중…" : "변경 이력 없음"} />
      </Card>

      {modal === "amend" && <AmendModal year={year} depts={depts.data?.items ?? []}
        accts={accts.data?.items ?? []} onDone={() => { setModal(null); refresh(); }}
        onError={fail} onClose={() => setModal(null)} />}
      {modal === "transfer" && <TransferModal year={year} depts={depts.data?.items ?? []}
        accts={accts.data?.items ?? []} onDone={() => { setModal(null); refresh(); }}
        onError={fail} onClose={() => setModal(null)} />}
      {modal === "carry" && <CarryModal depts={depts.data?.items ?? []}
        onDone={() => { setModal(null); refresh(); }} onError={fail} onClose={() => setModal(null)} />}
      {modal === "encumber" && <EncumberModal year={year} depts={depts.data?.items ?? []}
        accts={accts.data?.items ?? []} onDone={() => { setModal(null); refresh(); }}
        onError={fail} onClose={() => setModal(null)} />}
    </div>
  );
}

function DeptSelect({ depts, value, onChange }: {
  depts: Dept[]; value: string; onChange: (v: string) => void;
}) {
  return (
    <label className="block text-sm text-slate-600">부서
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        <option value="">선택…</option>
        {depts.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </select>
    </label>
  );
}

function AcctSelect({ accts, value, onChange, label = "계정과목" }: {
  accts: Acct[]; value: string; onChange: (v: string) => void; label?: string;
}) {
  return (
    <label className="block text-sm text-slate-600">{label}
      <select value={value} onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        <option value="">선택…</option>
        {accts.map((a) => <option key={a.code} value={a.code}>{a.code} {a.name}</option>)}
      </select>
    </label>
  );
}

function AmendModal({ year, depts, accts, onDone, onError, onClose }: {
  year: number; depts: Dept[]; accts: Acct[]; onDone: () => void;
  onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ department_id: "", account_code: "", amount: "", reason: "" });
  const m = useMutation({
    mutationFn: () => api("/finance/budgets", { method: "PUT", body: JSON.stringify({
      department_id: Number(f.department_id), fiscal_year: year, account_code: f.account_code,
      amount: Number(f.amount), reason: f.reason }) }),
    onSuccess: () => { toast.ok("예산 편성/변경 (이력 기록)"); onDone(); }, onError });
  const ok = f.department_id && f.account_code && f.amount !== "" && Number(f.amount) >= 0;
  return (
    <Modal title={`예산 편성·증액/감액 (${year})`} dirty onClose={onClose}>
      <div className="space-y-2">
        <DeptSelect depts={depts} value={f.department_id}
          onChange={(v) => setF({ ...f, department_id: v })} />
        <AcctSelect accts={accts} value={f.account_code}
          onChange={(v) => setF({ ...f, account_code: v })} />
        <Input label="편성 금액(총액)" value={f.amount} inputMode="numeric"
          onChange={(e) => setF({ ...f, amount: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="사유" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <Button disabled={m.isPending || !ok} onClick={() => m.mutate()}>저장</Button>
      </div>
    </Modal>
  );
}

function TransferModal({ year, depts, accts, onDone, onError, onClose }: {
  year: number; depts: Dept[]; accts: Acct[]; onDone: () => void;
  onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ department_id: "", from_account: "", to_account: "", amount: "", reason: "" });
  const m = useMutation({
    mutationFn: () => api("/finance/budgets/transfer", { method: "POST", body: JSON.stringify({
      department_id: Number(f.department_id), fiscal_year: year, from_account: f.from_account,
      to_account: f.to_account, amount: Number(f.amount), reason: f.reason }) }),
    onSuccess: () => { toast.ok("예산 전용 완료"); onDone(); }, onError });
  const ok = f.department_id && f.from_account && f.to_account && f.from_account !== f.to_account
    && Number(f.amount) > 0;
  return (
    <Modal title={`예산 전용(과목 간 이전) (${year})`} dirty onClose={onClose}>
      <div className="space-y-2">
        <DeptSelect depts={depts} value={f.department_id}
          onChange={(v) => setF({ ...f, department_id: v })} />
        <AcctSelect accts={accts} value={f.from_account} label="원 과목(감액)"
          onChange={(v) => setF({ ...f, from_account: v })} />
        <AcctSelect accts={accts} value={f.to_account} label="대상 과목(증액)"
          onChange={(v) => setF({ ...f, to_account: v })} />
        <Input label="전용 금액" value={f.amount} inputMode="numeric"
          onChange={(e) => setF({ ...f, amount: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="사유" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <Button disabled={m.isPending || !ok} onClick={() => m.mutate()}>전용 실행</Button>
      </div>
    </Modal>
  );
}

function CarryModal({ depts, onDone, onError, onClose }: {
  depts: Dept[]; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [dept, setDept] = useState("");
  const [fromYear, setFromYear] = useState(THIS_YEAR - 1);
  const m = useMutation({
    mutationFn: () => api<{ carried: boolean; total: number }>(
      `/finance/budgets/carry-over?department_id=${Number(dept)}&from_year=${fromYear}`,
      { method: "POST" }),
    onSuccess: (r) => { toast.ok(r.carried ? `이월 완료 — ${won(r.total)}` : "이월 대상 없음"); onDone(); },
    onError });
  return (
    <Modal title="예산 이월(전년 미집행 잔액)" onClose={onClose}>
      <div className="space-y-2">
        <DeptSelect depts={depts} value={dept} onChange={setDept} />
        <Input label="이월 원년도(from)" value={String(fromYear)} inputMode="numeric"
          onChange={(e) => setFromYear(Number(e.target.value.replace(/[^0-9]/g, "") || 0))} />
        <p className="text-xs text-slate-500">전년 미집행 잔액을 차년도({fromYear + 1})로 이월합니다(멱등).</p>
        <Button disabled={m.isPending || !dept} onClick={() => m.mutate()}>이월 실행</Button>
      </div>
    </Modal>
  );
}

function EncumberModal({ year, depts, accts, onDone, onError, onClose }: {
  year: number; depts: Dept[]; accts: Acct[]; onDone: () => void;
  onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ department_id: "", account_code: "", amount: "", description: "" });
  const m = useMutation({
    mutationFn: () => api("/finance/budgets/encumbrances", { method: "POST", body: JSON.stringify({
      department_id: Number(f.department_id), fiscal_year: year, account_code: f.account_code,
      amount: Number(f.amount), description: f.description }) }),
    onSuccess: () => { toast.ok("약정 등록 (가용예산 차감)"); onDone(); }, onError });
  const ok = f.department_id && f.account_code && Number(f.amount) > 0;
  return (
    <Modal title={`예산 약정 등록 (${year})`} dirty onClose={onClose}>
      <div className="space-y-2">
        <DeptSelect depts={depts} value={f.department_id}
          onChange={(v) => setF({ ...f, department_id: v })} />
        <AcctSelect accts={accts} value={f.account_code}
          onChange={(v) => setF({ ...f, account_code: v })} />
        <Input label="약정 금액(지출예정)" value={f.amount} inputMode="numeric"
          onChange={(e) => setF({ ...f, amount: e.target.value.replace(/[^0-9]/g, "") })} />
        <Input label="내역" value={f.description}
          onChange={(e) => setF({ ...f, description: e.target.value })} />
        <Button disabled={m.isPending || !ok} onClick={() => m.mutate()}>약정 등록</Button>
      </div>
    </Modal>
  );
}
