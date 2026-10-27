"use client";
/** 인사 — 급여 기초정보(보수 프로필) 온보딩: HIS/Clinic 동기화 직원의 ERP 보완입력. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Card, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import { AttachmentCell } from "@/components/attachment-cell";
import { won } from "@/lib/format";

interface Pending { employee_id: number; employee_no: string; name: string;
  source: string; employment_type: string; job_category: string | null; missing: string[] }
interface AllRow { employee_id: number; employee_no: string; name: string;
  employment_type: string; job_category: string | null; base_pay: number;
  dependents: number; bank_name: string | null; complete: boolean }
type EditTarget = { employee_id: number; employee_no: string; name: string };
interface Profile { registered?: boolean; employee_id: number; base_pay: number;
  dependents: number; bank_name: string | null; bank_account_masked: string | null }
interface RateHist { valid_from: string; amount: number; description: string | null;
  updated_by: number | null; updated_at: string | null }
interface PayRate { rate_key: string; amount: number | null; default: number | null;
  valid_from: string | null; history_count: number;
  overridden: boolean; description: string | null;
  next: { valid_from: string; amount: number } | null }
interface Dependent { id: number; name: string; relation: string;
  birth_date: string | null; disabled: boolean; counted: boolean }

const REL: Record<string, string> = { SPOUSE: "배우자", CHILD: "자녀", PARENT: "부모",
  GRANDPARENT: "조부모", SIBLING: "형제자매", OTHER: "기타" };

const MISSING_LABEL: Record<string, string> = {
  base_pay: "기본급", compensation_profile: "보수 프로필", bank_account: "급여계좌",
  contract_end: "계약만료일", daily_wage: "일용 일당",
};
const EMP_TYPE: Record<string, string> = {
  REGULAR: "정규", CONTRACT: "계약", DAILY: "일용", TEMP: "임시", WATCH: "감시단속",
};

export default function OnboardingPage() {
  const qc = useQueryClient();
  const { ask, dialog } = useInputDialog();
  const confirm = useConfirm();
  const [histKey, setHistKey] = useState<string | null>(null);
  const [sel, setSel] = useState<EditTarget | null>(null);
  const [form, setForm] = useState({ base_pay: "", dependents: "1", bank_name: "", bank_account: "" });
  const toast = useToast();
  const [view, setView] = useState<"pending" | "all">("pending");
  const [q, setQ] = useState("");

  const pending = useQuery({ queryKey: ["onboarding"], queryFn: () =>
    api<{ count: number; items: Pending[] }>("/hr/onboarding/pending") });
  const all = useQuery({ queryKey: ["onboarding-grid"], enabled: view === "all", queryFn: () =>
    api<{ count: number; incomplete: number; items: AllRow[] }>("/hr/onboarding/grid") });
  const allFiltered = (all.data?.items ?? []).filter((r) =>
    !q.trim() || r.name.includes(q.trim()) || r.employee_no.toLowerCase().includes(q.trim().toLowerCase()));

  const pick = async (p: EditTarget) => {
    setSel(p); 
    const cur = await api<Profile>(`/hr/compensation/${p.employee_id}`);
    setForm({ base_pay: cur.registered ? String(cur.base_pay) : "",
      dependents: cur.registered ? String(cur.dependents) : "1",
      bank_name: cur.bank_name ?? "", bank_account: "" });
  };

  const save = useMutation({
    mutationFn: () => api<Profile>(`/hr/compensation/${sel!.employee_id}`, {
      method: "PUT",
      body: JSON.stringify({
        base_pay: Number(form.base_pay), dependents: Number(form.dependents),
        bank_name: form.bank_name || null, bank_account: form.bank_account || null }) }),
    onSuccess: () => { toast.ok(`${sel!.name} 보수 프로필 저장 완료`); setSel(null);
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["onboarding-grid"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패"),
  });

  const rates = useQuery({ queryKey: ["payrates"], queryFn: () =>
    api<{ items: PayRate[] }>("/hr/pay-rates") });
  const saveRate = useMutation({
    mutationFn: (p: { key: string; amount: number }) =>
      api(`/hr/pay-rates/${p.key}`, { method: "PUT",
        // 발효일=오늘(새 기간 행 추가 — 과거 급여 재생성 미소급·이력 보존). 미래 발효 예약은 API.
        body: JSON.stringify({ amount: p.amount, valid_from: todayLocal() }) }),
    onSuccess: () => { toast.ok("단가 변경 완료 (감사기록)");
      qc.invalidateQueries({ queryKey: ["payrates"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "단가 변경 실패"),
  });
  // 발효일 이력 조회 + 오등록 행 삭제(과거 소급 왜곡 복구·감사기록)
  const hist = useQuery({ enabled: !!histKey, queryKey: ["payrate-hist", histKey],
    queryFn: () => api<{ items: RateHist[] }>(`/hr/pay-rates/${histKey}/history`) });
  const delRate = useMutation({
    mutationFn: (validFrom: string) =>
      api(`/hr/pay-rates/${histKey}/${validFrom}`, { method: "DELETE" }),
    onSuccess: () => { toast.ok("발효행 삭제 완료 (감사기록)");
      qc.invalidateQueries({ queryKey: ["payrate-hist", histKey] });
      qc.invalidateQueries({ queryKey: ["payrates"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "삭제 실패"),
  });

  const deps = useQuery({ enabled: !!sel, queryKey: ["deps", sel?.employee_id],
    queryFn: () => api<{ items: Dependent[]; payroll_dependents: number | null }>(
      `/hr/employees/${sel!.employee_id}/dependents`) });
  const [dep, setDep] = useState({ name: "", relation: "CHILD", birth_date: "" });
  const addDep = useMutation({
    mutationFn: () => api(`/hr/employees/${sel!.employee_id}/dependents`, {
      method: "POST", body: JSON.stringify({ ...dep, birth_date: dep.birth_date || null }) }),
    onSuccess: () => { setDep({ name: "", relation: "CHILD", birth_date: "" });
      qc.invalidateQueries({ queryKey: ["deps"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "가족 등록 실패"),
  });
  const delDep = useMutation({
    mutationFn: (id: number) => api(`/hr/dependents/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["deps"] }),
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "삭제 실패"),
  });

  const input = "w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm";
  return (
    <div className="space-y-4">
      {dialog}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">인사 — 급여 기초정보 온보딩</h1>
        <Link href="/hr/onboarding/bulk"
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50">
          일괄 편집 (스프레드시트)</Link>
      </div>
      <p className="text-sm text-slate-500">
        HIS/Clinic에서 등록·동기화된 직원의 급여 처리에 필요한 ERP 보완입력(기본급·부양가족·계좌) 체크리스트입니다.
        대량 편집은 우측 <b>일괄 편집(스프레드시트)</b>을 사용하세요.
      </p>

      <div className="flex items-center gap-2">
        {(["pending", "all"] as const).map((v) => (
          <button key={v} onClick={() => setView(v)}
            className={`rounded-md px-3 py-1.5 text-sm ${view === v
              ? "bg-blue-600 text-white" : "border border-slate-300 text-slate-600 hover:bg-slate-50"}`}>
            {v === "pending" ? "보완입력 대기" : "전체 직원 (개별 입력·수정)"}</button>
        ))}
      </div>

      {view === "pending" ? (
        <Card title={`보완입력 대기 (${pending.data?.count ?? 0}명)`}
          action={<span className="text-[11px] text-slate-400">재직자 중 급여 기초정보 누락</span>}>
          <Table headers={["사번", "이름", "출처", "고용형태", "직종", "누락 항목", ""]}
            empty="모든 재직자의 급여 기초정보가 완비되었습니다 — 개별 수정은 [전체 직원] 탭"
            rows={(pending.data?.items ?? []).map((p) => [
              p.employee_no, p.name, p.source, EMP_TYPE[p.employment_type] ?? p.employment_type,
              p.job_category ?? "-",
              <span key="m" className="text-rose-600">
                {p.missing.map((m) => MISSING_LABEL[m] ?? m).join(", ")}</span>,
              <button key="b" onClick={() => pick(p)}
                className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700">
                입력</button>,
            ])} />
        </Card>
      ) : (
        <Card title={`전체 직원 (${all.data?.count ?? 0}명 · 미완료 ${all.data?.incomplete ?? 0}명)`}
          action={<input value={q} onChange={(e) => setQ(e.target.value)} placeholder="이름·사번 검색"
            className="w-44 rounded-md border border-slate-300 px-2 py-1 text-sm" />}>
          <Table headers={["사번", "이름", "고용형태", "직종", "기본급", "상태", ""]}
            empty={all.isLoading ? "불러오는 중…" : "직원이 없습니다"}
            rows={allFiltered.map((r) => [
              r.employee_no, r.name, EMP_TYPE[r.employment_type] ?? r.employment_type, r.job_category ?? "-",
              r.base_pay ? won(r.base_pay)
                : <span key="np" className="text-rose-600">미입력</span>,
              r.complete ? <span key="c" className="text-emerald-600">완료</span>
                : <span key="c" className="text-rose-600">미완료</span>,
              <button key="e" onClick={() => pick(r)}
                className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700">
                입력·수정</button>,
            ])} />
        </Card>
      )}

      <Card title="수당 단가 (단협·취업규칙 — 발효일 기간 이력)" action={
        <span className="text-[11px] text-slate-400">변경=오늘 발효 새 이력 행(과거 급여 미소급·감사기록)</span>}>
        <Table headers={["항목", "설명", "현재 단가", "발효일", "기본값", ""]}
          rows={(rates.data?.items ?? []).map((r) => [
            <code key="k" className="text-xs">{r.rate_key}</code>,
            r.description ?? "-",
            <span key="a" className={r.overridden ? "font-semibold text-blue-700" : ""}>
              {r.amount != null ? won(r.amount) : "—"}</span>,
            <span key="v" className="text-xs text-slate-500">
              {r.valid_from ?? "-"}{r.history_count > 1 ? ` (이력 ${r.history_count})` : ""}
              {r.next && <span className="ml-1 text-emerald-600">
                · 예약 {r.next.valid_from} {won(r.next.amount)}</span>}</span>,
            r.default !== null ? won(r.default) : "-",
            <button key="e" className="rounded-md bg-slate-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-700"
              onClick={async () => { const v = await ask({ title: `${r.rate_key} 단가 변경`,
                label: "새 단가(원)", kind: "number", min: 0,
                defaultValue: String(r.amount ?? r.default ?? 0),
                confirmLabel: "변경" });
                if (v != null) saveRate.mutate({ key: r.rate_key, amount: Number(v) }); }}>
              변경</button>,
            <button key="h" className="ml-1 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-slate-50"
              onClick={() => setHistKey(r.rate_key)}>이력</button>,
          ])} />
      </Card>

      {histKey && (
        <Modal title={`${histKey} — 발효일 이력`} onClose={() => setHistKey(null)}>
          <p className="mb-2 text-xs text-slate-400">
            발효일 오등록(과거 발효일 오타·금액 오등록)은 삭제로 정정합니다. 삭제는 감사기록에 남으며,
            잘못된 과거 발효행이 이후 급여 재생성에 소급 개입하는 것을 막습니다.</p>
          <Table headers={["발효일", "단가", "설명", ""]}
            rows={(hist.data?.items ?? []).map((h) => [
              h.valid_from,
              <span key="a" className="tabular-nums">{won(h.amount)}</span>,
              h.description ?? "-",
              <button key="d" className="rounded-md bg-red-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
                disabled={delRate.isPending}
                onClick={async () => {
                  if (await confirm({ title: "발효행 삭제",
                    body: `${histKey} · ${h.valid_from} 발효 단가(${won(h.amount)})를 삭제할까요? 되돌릴 수 없습니다.`,
                    confirmLabel: "삭제", danger: true }))
                    delRate.mutate(h.valid_from); }}>
                삭제</button>,
            ])} empty="이력 없음(코드 기본값 사용)" />
        </Modal>
      )}

      {sel && (
        <Modal title={`${sel.name} (${sel.employee_no}) — 보수 프로필`}
          onClose={() => setSel(null)} size="lg">
          <div className="grid max-w-xl grid-cols-2 gap-3">
            <label className="text-sm text-slate-600">월 기본급(원)
              <input value={form.base_pay} inputMode="numeric" className={input}
                onChange={(e) => setForm({ ...form, base_pay: e.target.value })} />
            </label>
            <label className="text-sm text-slate-600">부양가족 수(본인 포함)
              <input value={form.dependents} inputMode="numeric" className={input}
                onChange={(e) => setForm({ ...form, dependents: e.target.value })} />
            </label>
            <label className="text-sm text-slate-600">은행
              <input value={form.bank_name} className={input}
                onChange={(e) => setForm({ ...form, bank_name: e.target.value })} />
            </label>
            <label className="text-sm text-slate-600">급여계좌 (암호화 저장·마스킹 표시)
              <input value={form.bank_account} placeholder="미입력 시 기존값 유지" className={input}
                onChange={(e) => setForm({ ...form, bank_account: e.target.value })} />
            </label>
          </div>
          <div className="mt-3 flex gap-2">
            <button disabled={save.isPending || !form.base_pay}
              onClick={() => Number.isFinite(Number(form.base_pay)) ? save.mutate() : toast.err("기본급은 숫자로 입력하세요")}
              className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50">
              저장
            </button>
            <button onClick={() => setSel(null)}
              className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-600 hover:bg-slate-50">
              닫기
            </button>
          </div>

          <div className="mt-5 border-t border-slate-100 pt-4">
            <p className="mb-2 text-sm font-medium text-slate-700">
              부양가족 (급여 부양가족 수: {deps.data?.payroll_dependents ?? "프로필 값 사용"}
              {deps.data?.payroll_dependents != null && " — 본인 포함, 산입분만"})
            </p>
            <Table headers={["이름", "관계", "생년월일", "산입", ""]}
              empty="등록된 가족 없음 — 미등록 시 보수 프로필의 부양가족 수 사용"
              rows={(deps.data?.items ?? []).map((d) => [
                d.name, REL[d.relation] ?? d.relation, d.birth_date ?? "-",
                d.counted ? "○" : <span className="text-slate-400">제외</span>,
                <button key="x" onClick={() => delDep.mutate(d.id)}
                  className="rounded bg-slate-400 px-2 py-0.5 text-xs text-white">삭제</button>,
              ])} />
            <div className="mt-2 flex flex-wrap items-end gap-2">
              <input value={dep.name} placeholder="이름"
                onChange={(e) => setDep({ ...dep, name: e.target.value })}
                className="w-28 rounded-md border border-slate-300 px-2 py-1 text-sm" />
              <select value={dep.relation}
                onChange={(e) => setDep({ ...dep, relation: e.target.value })}
                className="rounded-md border border-slate-300 px-2 py-1 text-sm">
                {Object.entries(REL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              <input type="date" value={dep.birth_date}
                onChange={(e) => setDep({ ...dep, birth_date: e.target.value })}
                className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
              <button disabled={!dep.name || addDep.isPending} onClick={() => addDep.mutate()}
                className="rounded-md bg-slate-600 px-3 py-1 text-xs font-medium text-white disabled:opacity-50">
                가족 추가 (공제대상 자동 제안)</button>
            </div>
          </div>

          <div className="mt-5 border-t border-slate-100 pt-4">
            <p className="mb-2 text-sm font-medium text-slate-700">등록 서류 (통장사본·신분증·증빙 등)</p>
            <div className="flex items-center gap-2 text-sm text-slate-600">
              <span>📎 첨부 / 다운로드:</span>
              <AttachmentCell entityType="employee" entityId={sel.employee_id} />
            </div>
            <p className="mt-2 text-[11px] text-slate-400">
              PDF·이미지·문서, 파일당 20MB. 동일 파일 자동 중복제거. 직원 서류함(entity=employee)과 공유됩니다.</p>
          </div>
        </Modal>
      )}
    </div>
  );
}
