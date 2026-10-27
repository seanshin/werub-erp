"use client";
/** 원무 — 진료의뢰·회송 (진료협력센터·P3-3): 의뢰(REFER_OUT)·의뢰접수(REFER_IN)·회송(BACK_REFER)
 *  기록·의뢰회송료·회송률 실적. 전 직원(진료협력 담당). 임상 의뢰서 작성은 진료(HIS). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { todayLocal, monthLocal } from "@/lib/dates";
import { useToast } from "@/components/toast";

const TYPE: Record<string, { kr: string; tone: "blue" | "green" | "amber" }> = {
  REFER_OUT: { kr: "진료의뢰(송부)", tone: "blue" }, REFER_IN: { kr: "의뢰접수(초진)", tone: "green" },
  BACK_REFER: { kr: "회송", tone: "amber" } };

interface Referral {
  id: number; referral_type: string; partner_institution: string; chart_no: string | null;
  patient_name: string | null; department: string | null; referred_on: string;
  reason: string | null; has_fee: boolean; status: string; note: string | null;
}
interface Stats {
  start: string; end: string; by_type: Record<string, { count: number; fee: number }>;
  total: number; fee_total: number; back_refer_rate: number | null;
}

export default function ReferralsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [typeF, setTypeF] = useState("");
  const [creating, setCreating] = useState(false);
  const start = `${monthLocal()}-01`;
  const end = todayLocal();

  const list = useQuery({ queryKey: ["referrals", typeF], queryFn: () =>
    api<{ items: Referral[] }>(`/patients/referrals${typeF ? `?referral_type=${typeF}` : ""}`) });
  const stats = useQuery({ queryKey: ["referral-stats", start, end], queryFn: () =>
    api<Stats>(`/patients/referrals/stats?start=${start}&end=${end}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["referrals"] });
    qc.invalidateQueries({ queryKey: ["referral-stats"] }); };

  const close = useMutation({
    mutationFn: (id: number) => api(`/patients/referrals/${id}/close`, { method: "POST" }),
    onSuccess: () => { toast.ok("종결"); refresh(); }, onError: fail });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">원무 — 진료의뢰·회송</h1>
        <Button onClick={() => setCreating(true)}>의뢰·회송 등록</Button>
      </div>
      <p className="text-xs text-slate-500">
        진료협력센터 의뢰(타기관 송부)·의뢰접수(초진)·회송 기록과 의뢰회송료·회송률 실적입니다.
        임상 의뢰서 작성은 진료(HIS), 본 화면은 실적·수가 관리입니다.</p>

      <QueryErrorBanner queries={[list, stats]} />

      {stats.data && (
        <Card title={`이달 실적 (${stats.data.start} ~ ${stats.data.end})`}>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            {(["REFER_OUT", "REFER_IN", "BACK_REFER"] as const).map((t) => (
              <div key={t} className="rounded-lg border border-slate-200 p-2">
                <p className="text-xs text-slate-500">{TYPE[t].kr}</p>
                <p className="text-sm font-bold">{stats.data!.by_type[t]?.count ?? 0}건</p>
                <p className="text-[11px] text-slate-400">수가 {stats.data!.by_type[t]?.fee ?? 0}</p>
              </div>))}
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-2">
              <p className="text-xs text-slate-500">회송률(근사)</p>
              <p className="text-sm font-bold text-amber-700">{stats.data.back_refer_rate ?? "—"}%</p>
              <p className="text-[11px] text-slate-400">회송/의뢰접수</p></div>
            <div className="rounded-lg border border-slate-300 bg-slate-50 p-2">
              <p className="text-xs text-slate-500">의뢰회송료</p>
              <p className="text-sm font-bold">{stats.data.fee_total}건</p></div>
          </div>
        </Card>
      )}

      <Card title="의뢰·회송 목록" action={
        <select value={typeF} onChange={(e) => setTypeF(e.target.value)}
          className="rounded border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체 유형</option>
          {Object.entries(TYPE).map(([k, v]) => <option key={k} value={k}>{v.kr}</option>)}
        </select>}>
        <Table headers={["일자", "유형", "상대기관", "차트", "진료과", "사유", "수가", "상태", ""]}
          rows={(list.data?.items ?? []).map((r) => [
            r.referred_on,
            <Badge key="t" tone={(TYPE[r.referral_type] ?? { tone: "blue" as const }).tone}>
              {(TYPE[r.referral_type] ?? { kr: r.referral_type }).kr}</Badge>,
            r.partner_institution, r.chart_no ?? "—", r.department ?? "—",
            <span key="rs" className="text-xs">{r.reason ?? "—"}</span>,
            r.has_fee ? <Badge key="f" tone="green">청구</Badge> : "—",
            <Badge key="s" tone={r.status === "CLOSED" ? "gray" : "blue"}>
              {r.status === "CLOSED" ? "종결" : "진행"}</Badge>,
            r.status === "OPEN"
              ? <Button key="c" size="sm" variant="secondary" disabled={close.isPending}
                  onClick={() => close.mutate(r.id)}>종결</Button> : "",
          ])} empty={list.isFetching ? "불러오는 중…" : "기록 없음"} />
      </Card>

      {creating && <CreateModal onDone={() => { setCreating(false); refresh(); }}
        onError={fail} onClose={() => setCreating(false)} />}
    </div>
  );
}

function CreateModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ referral_type: "REFER_OUT", partner_institution: "",
    referred_on: todayLocal(), chart_no: "", patient_name: "", department: "", reason: "",
    has_fee: false, note: "" });
  const m = useMutation({
    mutationFn: () => api("/patients/referrals", { method: "POST", body: JSON.stringify({
      referral_type: f.referral_type, partner_institution: f.partner_institution,
      referred_on: f.referred_on, chart_no: f.chart_no || null,
      patient_name: f.patient_name || null, department: f.department || null,
      reason: f.reason || null, has_fee: f.has_fee, note: f.note || null }) }),
    onSuccess: () => { toast.ok("등록"); onDone(); }, onError });
  return (
    <Modal title="진료의뢰·회송 등록" dirty onClose={onClose}>
      <div className="space-y-2">
        <label className="block text-sm text-slate-600">유형
          <select value={f.referral_type} onChange={(e) => setF({ ...f, referral_type: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(TYPE).map(([k, v]) => <option key={k} value={k}>{v.kr}</option>)}
          </select>
        </label>
        <Input label="상대 의료기관" value={f.partner_institution}
          onChange={(e) => setF({ ...f, partner_institution: e.target.value })} />
        <Input label="일자" type="date" value={f.referred_on}
          onChange={(e) => setF({ ...f, referred_on: e.target.value })} />
        <div className="grid grid-cols-2 gap-2">
          <Input label="차트번호(선택)" value={f.chart_no}
            onChange={(e) => setF({ ...f, chart_no: e.target.value })} />
          <Input label="진료과(선택)" value={f.department}
            onChange={(e) => setF({ ...f, department: e.target.value })} />
        </div>
        <Input label="사유(선택)" value={f.reason}
          onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input type="checkbox" checked={f.has_fee}
            onChange={(e) => setF({ ...f, has_fee: e.target.checked })} />
          의뢰회송료 청구
        </label>
        <Button disabled={m.isPending || !f.partner_institution.trim()} onClick={() => m.mutate()}>
          등록</Button>
      </div>
    </Modal>
  );
}
