"use client";
/** 감사·이상탐지 패널 (ADR D-7) — Benford·중복전표·비정상 분개 + 검토완료 워크플로.
 *  결과는 모두 "검토 후보"이며 자동 판정이 아님(감사 윤리: 중립 표기). */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Spinner, Table } from "@/components/ui";
import { QueryError } from "@/components/query-guard";
import { GroupedBarChart } from "@/components/charts";
import { dt, man } from "@/lib/format";


export interface Review {
  id: number; status: string; disposition: "NORMAL" | "ACTION" | null;
  note: string | null; reviewed_by: number | null; reviewed_at: string | null;
}
type FindingType = "ABNORMAL" | "DUPLICATE";

// ── 검토완료 컨트롤 (행 공용) ────────────────────────────────────────────────
const DISP_KR: Record<string, { label: string; tone: "green" | "gray" }> = {
  NORMAL: { label: "정상확인", tone: "green" }, ACTION: { label: "조치필요", tone: "gray" },
};

function ReviewControl({ findingType, findingKey, review }: {
  findingType: FindingType; findingKey: string; review: Review | null;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [disposition, setDisposition] = useState<"NORMAL" | "ACTION">("NORMAL");
  const [note, setNote] = useState("");
  const [err, setErr] = useState("");
  const done = () => { qc.invalidateQueries({ queryKey: ["audit"] }); setOpen(false); setNote(""); };
  const fail = (e: unknown) => setErr(e instanceof ApiError ? e.error.message : "처리 실패");

  const save = useMutation({
    mutationFn: () => api("/analytics/audit/reviews", { method: "POST",
      body: JSON.stringify({ finding_type: findingType, finding_key: findingKey, disposition, note: note || null }) }),
    onSuccess: done, onError: fail });
  const cancel = useMutation({
    mutationFn: () => api(`/analytics/audit/reviews/${review?.id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["audit"] }), onError: fail });

  if (review) {
    const d = DISP_KR[review.disposition ?? "NORMAL"];
    return (
      <span className="inline-flex items-center gap-1.5" title={review.note ?? undefined}>
        <Badge tone={d.tone}>{d.label}</Badge>
        <button onClick={() => cancel.mutate()}
          className="text-[11px] text-slate-400 hover:text-slate-600 hover:underline">취소</button>
      </span>
    );
  }
  return (
    <>
      <button onClick={() => { setErr(""); setOpen(true); }}
        className="text-[11px] font-medium text-blue-600 hover:underline">검토완료</button>
      {open && (
        <Modal title="검토완료 처리" size="sm" onClose={() => setOpen(false)}>
          <div className="space-y-3 text-sm">
            <p className="text-slate-500">이 후보의 검토 결과를 기록합니다. 자동 판정이 아닙니다.</p>
            <div className="flex gap-2">
              {(["NORMAL", "ACTION"] as const).map((v) => (
                <button key={v} onClick={() => setDisposition(v)}
                  className={`flex-1 rounded-md border px-3 py-1.5 text-sm ${
                    disposition === v ? "border-blue-500 bg-blue-50 font-semibold text-blue-700"
                      : "border-slate-300 text-slate-600 hover:bg-slate-50"}`}>
                  {DISP_KR[v].label}{v === "NORMAL" ? " (오탐·정상)" : " (후속 필요)"}
                </button>
              ))}
            </div>
            <Input label="메모 (선택)" value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="예: 정기 비품 구매로 확인" />
            {err && <p className="text-red-600">{err}</p>}
            <div className="flex justify-end gap-2 pt-1">
              <Button variant="secondary" onClick={() => setOpen(false)}>닫기</Button>
              <Button onClick={() => save.mutate()} isLoading={save.isPending}>저장</Button>
            </div>
          </div>
        </Modal>
      )}
    </>
  );
}

// ── ① Benford ────────────────────────────────────────────────────────────────
interface BenfordDigit {
  digit: number; observed: number; observed_pct: number; expected_pct: number; deviation_pct: number;
}
interface Benford {
  side: string | null; sample_size: number; chi_square: number; chi_square_critical: number;
  chi_square_exceeds: boolean; mad: number; verdict: string; digits: BenfordDigit[];
  top_deviations: BenfordDigit[]; note: string | null;
}
const VERDICT_TONE: Record<string, "green" | "blue" | "amber" | "red"> = {
  "근접 부합": "green", "허용 가능": "blue", "한계": "amber", "부적합": "red",
};

export function BenfordPanel({ from, to }: { from: string; to: string }) {
  const [side, setSide] = useState<"DEBIT" | "CREDIT" | "">("DEBIT");
  const [detail, setDetail] = useState(false);
  const q = useQuery({ queryKey: ["audit", "benford", from, to, side],
    queryFn: () => api<Benford>(
      `/analytics/audit/benford?date_from=${from}&date_to=${to}${side ? `&side=${side}` : ""}`) });
  const b = q.data;
  const SIDES: [string, string][] = [["DEBIT", "차변"], ["CREDIT", "대변"], ["", "전체"]];

  return (
    <Card title="① Benford 법칙 분석" action={
      <div className="flex gap-1">
        {SIDES.map(([v, l]) => (
          <button key={v} onClick={() => setSide(v as "DEBIT" | "CREDIT" | "")}
            className={`rounded px-2 py-0.5 text-xs ${side === v
              ? "bg-blue-50 font-semibold text-blue-700" : "text-slate-500 hover:bg-slate-100"}`}>{l}</button>
        ))}
      </div>}>
      {q.isError ? <QueryError error={q.error} onRetry={() => q.refetch()} /> : q.isLoading ? <Spinner label="분석 중…" /> : !b ? null : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span>부합도 <Badge tone={VERDICT_TONE[b.verdict] ?? "gray"}>{b.verdict}</Badge></span>
            <span className="text-slate-600">표본 {b.sample_size.toLocaleString()}건</span>
            <span className={b.chi_square_exceeds ? "text-amber-700" : "text-slate-600"}>
              χ² {b.chi_square} <span className="text-slate-400">(임계 {b.chi_square_critical})</span></span>
            <span className="text-slate-600">MAD {b.mad}</span>
          </div>
          <GroupedBarChart format={(n) => `${n}%`} data={b.digits.map((d) => ({
            label: String(d.digit),
            bars: [{ value: d.expected_pct, color: "#cbd5e1" }, { value: d.observed_pct, color: "#3b82f6" }] }))} />
          <div className="flex items-center gap-3 text-[11px] text-slate-500">
            <span className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm bg-slate-300" />기대</span>
            <span className="inline-flex items-center gap-1"><i className="inline-block h-2.5 w-2.5 rounded-sm bg-blue-500" />실측</span>
            <button onClick={() => setDetail(true)} className="ml-auto text-blue-600 hover:underline">자릿수 상세 ▸</button>
          </div>
          {b.note && <p className="rounded bg-amber-50/60 px-2 py-1 text-[11px] text-amber-700">{b.note}</p>}
          <p className="text-[11px] text-slate-400">Benford는 금액이 여러 자릿수에 분포할 때 유효합니다. 결과는 검토 신호이며 판정이 아닙니다.</p>
        </div>
      )}
      {detail && b && (
        <Modal title="Benford 자릿수별 편차" size="lg" onClose={() => setDetail(false)}>
          <Table headers={["첫자리", "실측건수", "실측%", "기대%", "편차%p"]}
            rows={[...b.digits].sort((a, c) => Math.abs(c.deviation_pct) - Math.abs(a.deviation_pct)).map((d) => [
              String(d.digit), d.observed.toLocaleString(), `${d.observed_pct}%`, `${d.expected_pct}%`,
              <span key="dev" className={Math.abs(d.deviation_pct) >= 2 ? "font-semibold text-amber-700" : ""}>
                {d.deviation_pct > 0 ? "+" : ""}{d.deviation_pct}%p</span>])} />
        </Modal>
      )}
    </Card>
  );
}

// ── ② 중복전표 ────────────────────────────────────────────────────────────────
interface DupVoucher { voucher_id: number; voucher_on: string; source: string }
interface Cluster {
  finding_key: string; account_code: string; side: string; amount: number; detail: string | null;
  count: number; span_days: number; vouchers: DupVoucher[]; review: Review | null;
}
interface Dups { cluster_count: number; open_count: number; duplicate_amount: number; clusters: Cluster[] }

export function DuplicatePanel({ from, to }: { from: string; to: string }) {
  const [unreviewed, setUnreviewed] = useState(true);
  const [sel, setSel] = useState<Cluster | null>(null);
  const q = useQuery({ queryKey: ["audit", "duplicates", from, to, unreviewed],
    queryFn: () => api<Dups>(
      `/analytics/audit/duplicates?date_from=${from}&date_to=${to}&unreviewed_only=${unreviewed}`) });
  const d = q.data;

  return (
    <Card title="② 중복전표·중복지급" action={
      <label className="flex items-center gap-1 text-xs text-slate-500">
        <input type="checkbox" checked={unreviewed} onChange={(e) => setUnreviewed(e.target.checked)} />미검토만</label>}>
      {q.isError ? <QueryError error={q.error} onRetry={() => q.refetch()} /> : q.isLoading ? <Spinner label="분석 중…" /> : !d ? null : (
        <div className="space-y-2">
          <p className="text-xs text-slate-500">동일 계정·금액·거래처(적요)가 기간 내 2건 이상 · 의심 누적 {man(d.duplicate_amount)}</p>
          <Table headers={["계정", "금액", "건수", "기간", "거래처", "검토", ""]}
            empty="중복 후보가 없습니다"
            rows={d.clusters.map((c) => [
              c.account_code, man(c.amount), `${c.count}건`, `${c.span_days}일`, c.detail ?? "-",
              <ReviewControl key="r" findingType="DUPLICATE" findingKey={c.finding_key} review={c.review} />,
              <button key="v" onClick={() => setSel(c)} className="text-[11px] text-blue-600 hover:underline">상세 ▸</button>])} />
        </div>
      )}
      {sel && (
        <Modal title={`중복 후보 — ${sel.account_code} / ${man(sel.amount)}`} size="lg" onClose={() => setSel(null)}>
          <p className="mb-2 text-sm text-slate-500">거래처: {sel.detail ?? "-"} · {sel.count}건 ({sel.span_days}일 내)</p>
          <Table headers={["전표ID", "회계일", "출처"]}
            rows={sel.vouchers.map((v) => [`#${v.voucher_id}`, v.voucher_on, v.source])} />
        </Modal>
      )}
    </Card>
  );
}

// ── ③ 비정상 분개 ─────────────────────────────────────────────────────────────
interface Abn {
  finding_key: string; voucher_id: number; voucher_on: string; amount: number;
  source: string; memo: string | null; reasons: string[]; review: Review | null;
}
interface Abnormal {
  threshold: number | null; flagged_count: number; open_count: number;
  by_reason: Record<string, number>; items: Abn[];
}
const REASON_KR: Record<string, { label: string; tone: "blue" | "amber" | "gray" | "red" }> = {
  weekend: { label: "주말", tone: "blue" }, offhours: { label: "심야", tone: "amber" },
  round: { label: "라운드넘버", tone: "gray" }, below_threshold: { label: "한도직전", tone: "red" },
};

export function AbnormalPanel({ from, to }: { from: string; to: string }) {
  const [unreviewed, setUnreviewed] = useState(true);
  const [threshold, setThreshold] = useState("");
  const [filter, setFilter] = useState("");
  const [sel, setSel] = useState<Abn | null>(null);
  const q = useQuery({ queryKey: ["audit", "abnormal", from, to, threshold, unreviewed],
    queryFn: () => api<Abnormal>(
      `/analytics/audit/abnormal?date_from=${from}&date_to=${to}&unreviewed_only=${unreviewed}` +
      (threshold ? `&threshold=${threshold}` : "")) });
  const a = q.data;
  const items = useMemo(
    () => (a?.items ?? []).filter((it) => !filter || it.reasons.includes(filter)), [a, filter]);
  const CHIPS: [string, string][] = [["", "전체"], ...Object.keys(REASON_KR).map(
    (k) => [k, REASON_KR[k].label] as [string, string])];

  return (
    <Card title="③ 비정상 분개" action={
      <label className="flex items-center gap-1 text-xs text-slate-500">
        <input type="checkbox" checked={unreviewed} onChange={(e) => setUnreviewed(e.target.checked)} />미검토만</label>}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <div className="flex flex-wrap gap-1">
          {CHIPS.map(([v, l]) => (
            <button key={v} onClick={() => setFilter(v)}
              className={`rounded-full px-2.5 py-0.5 text-xs ${filter === v
                ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>{l}</button>
          ))}
        </div>
        <label className="ml-auto flex items-center gap-1 text-xs text-slate-500">승인한도(원)
          <input type="number" value={threshold} onChange={(e) => setThreshold(e.target.value)}
            placeholder="예: 5000000" className="w-28 rounded border border-slate-300 px-2 py-0.5 text-xs" /></label>
      </div>
      <p className="mb-2 text-[11px] text-slate-400">수동 입력 전표만 · 주말·심야는 입력시각(KST) 기준 · 자동전표(배치) 제외</p>
      {q.isError ? <QueryError error={q.error} onRetry={() => q.refetch()} /> : q.isLoading ? <Spinner label="분석 중…" /> : (
        <Table headers={["전표", "입력/회계일", "금액", "사유", "적요", "검토", ""]}
          empty="비정상 후보가 없습니다"
          rows={items.map((it) => [
            `#${it.voucher_id}`, it.voucher_on, man(it.amount),
            <span key="rs" className="flex flex-wrap gap-1">{it.reasons.map((r) => (
              <Badge key={r} tone={REASON_KR[r]?.tone ?? "gray"}>{REASON_KR[r]?.label ?? r}</Badge>))}</span>,
            it.memo ?? "-",
            <ReviewControl key="r" findingType="ABNORMAL" findingKey={it.finding_key} review={it.review} />,
            <button key="v" onClick={() => setSel(it)} className="text-[11px] text-blue-600 hover:underline">상세 ▸</button>])} />
      )}
      {sel && (
        <Modal title={`전표 #${sel.voucher_id}`} size="sm" onClose={() => setSel(null)}>
          <dl className="space-y-1.5 text-sm">
            <div className="flex justify-between"><dt className="text-slate-500">회계일</dt><dd>{sel.voucher_on}</dd></div>
            <div className="flex justify-between"><dt className="text-slate-500">금액</dt><dd>{man(sel.amount)}</dd></div>
            <div className="flex justify-between"><dt className="text-slate-500">출처</dt><dd>{sel.source}</dd></div>
            <div className="flex justify-between"><dt className="text-slate-500">적요</dt><dd>{sel.memo ?? "-"}</dd></div>
            <div className="flex justify-between gap-2"><dt className="text-slate-500">사유</dt>
              <dd className="flex flex-wrap justify-end gap-1">{sel.reasons.map((r) => (
                <Badge key={r} tone={REASON_KR[r]?.tone ?? "gray"}>{REASON_KR[r]?.label ?? r}</Badge>))}</dd></div>
            {sel.review?.reviewed_at && <div className="flex justify-between"><dt className="text-slate-500">검토</dt>
              <dd>{dt(sel.review.reviewed_at)}</dd></div>}
          </dl>
        </Modal>
      )}
    </Card>
  );
}
