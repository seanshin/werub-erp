"use client";
/** 의료보험 — 사전심사 (FR-41 + PR-A 워크리스트):
 *  실행·평가(룰/보조) + 차트 케이스 워크리스트(배정·진행·적발 결정·코멘트). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Modal, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import {
  type ApiLine, AutoActionsTab, BulkSheetCard, CaseSheetView, QueriesTab, RulesTab, SheetInputCard,
  StatementView, StatsTab,
} from "./extras";

interface Line { chart_no: string; code: string; count: number; amount: string; treated_on: string }
interface Finding { rule_name: string; severity: string; chart_no: string; code: string; detail: string }
interface Result { count: number; blocked: boolean; findings: Finding[];
  worklist?: { cases: { case_id: number; chart_no: string; status: string; findings: number }[];
    auto_resolved?: number; auto_cleared?: number; auto_dismissed?: number } }
interface CaseRow { id: number; chart_no: string; care_month: string; status: string;
  encounter_type: string | null;
  max_severity: string | null; risk_amount: number; claimed_amt: number;
  finding_count: number; block_count?: number; warn_count?: number;
  auto_cleared?: boolean; assignee_id: number | null }
interface CaseFinding { id: number; rule_code: string; rule_name: string | null;
  severity: string; code: string | null; treated_on: string | null; detail: string | null;
  reason_group?: string | null; reason_code?: string | null;
  amount: number; decision: string; decision_note: string | null; resolved: boolean;
  auto_decided?: boolean; physician_gated?: boolean }
interface AiItem { finding_id: number; available: boolean; held?: boolean; note?: string;
  risk_level?: string | null; recommendation?: string;
  evidence_citations?: { title?: string; relevant_passage?: string }[];
  false_positive_candidate?: boolean; dismiss_reason_draft?: string }   // A3 오탐 사전제안
interface CaseDetail extends CaseRow {
  findings: CaseFinding[]; comments: { id: number; author: number; body: string; created_at: string }[];
  adjustment_history: { count: number; total_amt: number;
    top_categories: { category: string; count: number }[] } }
interface NoticeHit { erp_ref_id: string; notice_type: string; title: string;
  effective_from: string; score: number; snippet: string }

const EMPTY: Line = { chart_no: "", code: "", count: 1, amount: "", treated_on: "" };
const STATUS: Record<string, { label: string; tone: "gray" | "blue" | "green" | "amber" | "red" }> = {
  OPEN: { label: "대기", tone: "gray" }, IN_REVIEW: { label: "검토중", tone: "blue" },
  CLEARED: { label: "이상없음", tone: "green" }, FIX_REQUESTED: { label: "수정요청", tone: "amber" },
  BLOCKED: { label: "청구보류", tone: "red" }, RESOLVED: { label: "해소", tone: "green" },
};
const NEXT: Record<string, string[]> = {
  OPEN: ["IN_REVIEW", "CLEARED", "BLOCKED", "FIX_REQUESTED"],
  IN_REVIEW: ["CLEARED", "FIX_REQUESTED", "BLOCKED", "OPEN"],
  FIX_REQUESTED: ["IN_REVIEW", "CLEARED"], BLOCKED: ["IN_REVIEW", "FIX_REQUESTED", "CLEARED"],
  CLEARED: ["IN_REVIEW"], RESOLVED: ["IN_REVIEW"],
};
// 진료일 ±N일 이동(전일/다음일). 빈 값이면 오늘 기준. 로컬 날짜 문자열 YYYY-MM-DD.
const shiftDay = (d: string, delta: number) => {
  const base = d ? new Date(`${d}T00:00:00`) : new Date();
  base.setDate(base.getDate() + delta);
  return `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`;
};
const ENC: Record<string, { label: string; tone: "blue" | "red" | "amber" }> = {
  OUTPATIENT: { label: "외래", tone: "blue" }, INPATIENT: { label: "입원", tone: "red" },
  MIXED: { label: "혼합", tone: "amber" },
};

export default function PreReviewPage() {
  const qc = useQueryClient();
  const [tab, setTab] = useState<"worklist" | "run" | "stats" | "rules" | "queries" | "auto">("worklist");
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  // ── 워크리스트 ──────────────────────────────────────
  const [filter, setFilter] = useState<{ status: string; severity: string; chart: string;
    enc: string; day: string }>({ status: "", severity: "", chart: "", enc: "", day: "" });
  const [selId, setSelId] = useState<number | null>(null);
  const [stmtId, setStmtId] = useState<number | null>(null);
  const [allMode, setAllMode] = useState(false);   // all=검토+명세 함께 팝업
  const [monthTab, setMonthTab] = useState<string | null>(null);  // null=최신월, ""=전체
  // POST 검색(Phase0 W1) — chart_no가 쿼리스트링으로 access log에 남지 않도록
  const cases = useQuery({ queryKey: ["prc", filter], queryFn: () =>
    api<{ count: number; items: CaseRow[] }>("/claims/pre-review/cases/search", {
      method: "POST",
      body: JSON.stringify({
        status: filter.status || null, severity: filter.severity || null,
        chart_no: filter.chart || null, encounter_type: filter.enc || null,
        treated_on: filter.day || null }),
    }) });
  const detail = useQuery({ enabled: selId !== null, queryKey: ["prc-detail", selId],
    queryFn: () => api<CaseDetail>(`/claims/pre-review/cases/${selId}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["prc"] });
    qc.invalidateQueries({ queryKey: ["prc-detail"] }); };

  const updateCase = useMutation({
    mutationFn: (p: { id: number; status?: string; assignee_id?: number }) =>
      api(`/claims/pre-review/cases/${p.id}`, { method: "PATCH",
        body: JSON.stringify({ status: p.status, assignee_id: p.assignee_id }) }),
    onSuccess: () => { toast.ok("케이스 갱신 완료"); refresh(); }, onError: fail });
  const decide = useMutation({
    mutationFn: (p: { id: number; decision: string; note?: string }) =>
      api(`/claims/pre-review/findings/${p.id}/decision`, { method: "PATCH",
        body: JSON.stringify({ decision: p.decision, note: p.note }) }),
    onSuccess: () => { toast.ok("적발 결정 기록 완료"); refresh(); }, onError: fail });
  // 오탐(DISMISS) 사유 인라인 입력 — 브라우저 prompt 대체
  const [dismiss, setDismiss] = useState<{ id: number; note: string } | null>(null);
  const submitDismiss = () => {
    if (!dismiss || !dismiss.note.trim()) return;
    decide.mutate({ id: dismiss.id, decision: "DISMISS", note: dismiss.note.trim() });
    setDismiss(null);
  };
  const [memo, setMemo] = useState("");
  const comment = useMutation({
    mutationFn: (p: { id: number; body: string }) =>
      api(`/claims/pre-review/cases/${p.id}/comments`, { method: "POST",
        body: JSON.stringify({ body: p.body }) }),
    onSuccess: () => { setMemo(""); refresh(); }, onError: fail });   // 성공 시에만 초기화(실패 시 메모 보존)

  // 심사 보조 검토(P1) — 케이스의 미해소 적발을 weve로 일괄 진단(휴먼인더루프 참고)
  const [aiReview, setAiReview] = useState<Record<number, AiItem>>({});
  const aiReviewMut = useMutation({
    mutationFn: (cid: number) => api<{ available: boolean; items: AiItem[] }>(
      `/claims/pre-review/cases/${cid}/ai-review`, { method: "POST" }),
    onSuccess: (r) => {
      if (!r.available) { toast.info("보조 평가 미연결(weve 키 미설정)"); return; }
      setAiReview(Object.fromEntries(r.items.map((it) => [it.finding_id, it])));
      toast.ok(`심사 보조 검토 완료 — ${r.items.length}건 (참고 · 결정은 심사자)`);
    },
    onError: fail });

  // ── 월별(진료월) 탭 — 케이스를 care_month 단위로 구성 ───────
  const allCases = cases.data?.items ?? [];
  const monthCounts = allCases.reduce<Record<string, number>>((a, c) => {
    a[c.care_month] = (a[c.care_month] ?? 0) + 1; return a; }, {});
  const months = Object.keys(monthCounts).sort().reverse();   // 최신월 우선
  const activeMonth = monthTab === null ? (months[0] ?? "") : monthTab;  // ""=전체
  const monthItems = activeMonth === "" ? allCases
    : allCases.filter((c) => c.care_month === activeMonth);

  // ── 클라이언트 페이지네이션(대량 워크리스트 렌더 경감) ──
  const PAGE_SIZE = 50;
  const [page, setPage] = useState(1);
  // 필터/월 변경 시 1페이지로 — effect 대신 렌더 중 상태 조정(React 권장 패턴)
  const pageKey = `${activeMonth}|${filter.status}|${filter.day}`;
  const [prevPageKey, setPrevPageKey] = useState(pageKey);
  if (pageKey !== prevPageKey) { setPrevPageKey(pageKey); setPage(1); }
  const pageCount = Math.max(1, Math.ceil(monthItems.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount);
  const pageItems = monthItems.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  // 시트 일괄 작업에 넘길 필터 요약 + 반출 쿼리(목록과 동일 대상)
  const fparts: string[] = [activeMonth ? `진료월 ${activeMonth}` : "전체 월"];
  if (filter.day) fparts.push(`진료일 ${filter.day}`);
  if (filter.status) fparts.push(`상태 ${STATUS[filter.status]?.label ?? filter.status}`);
  if (filter.severity) fparts.push(`심각도 ${filter.severity}`);
  if (filter.enc) fparts.push(ENC[filter.enc]?.label ?? filter.enc);
  if (filter.chart) fparts.push(`차트 "${filter.chart}"`);
  const filterLabel = fparts.join(" · ");
  const eqp = new URLSearchParams();
  if (filter.status) eqp.set("status", filter.status);
  if (filter.severity) eqp.set("severity", filter.severity);
  if (filter.enc) eqp.set("encounter_type", filter.enc);
  if (filter.chart) eqp.set("chart_no", filter.chart);
  if (filter.day) eqp.set("treated_on", filter.day);
  if (activeMonth) eqp.set("care_month", activeMonth);
  const exportQuery = eqp.toString();

  // ── 실행·평가 (기존) ─────────────────────────────────
  const [lines, setLines] = useState<Line[]>([{ ...EMPTY }]);
  const [result, setResult] = useState<Result | null>(null);
  const run = useMutation({
    mutationFn: (p: { save: boolean; apiLines?: ApiLine[] }) =>
      api<Result>("/claims/pre-review", { method: "POST", body: JSON.stringify({
        save: p.save,
        lines: p.apiLines ?? lines.filter((l) => l.code).map((l) => ({
          chart_no: l.chart_no, code: l.code, count: Number(l.count) || 1,
          amount: l.amount ? Number(l.amount) : null, treated_on: l.treated_on || null })) }) }),
    onSuccess: (r) => { setResult(r);
      if (r.worklist) {
        const ac = r.worklist.auto_cleared ?? 0, ad = r.worklist.auto_dismissed ?? 0;
        toast.ok(`워크리스트 저장 — 케이스 ${r.worklist.cases.length}건`
          + (ac > 0 ? ` · 🤖 자동 이상없음 ${ac}건` : "")
          + (ad > 0 ? ` · 🤖 자동 오탐 ${ad}건` : "")); refresh();
      }
      else if (r.count === 0) toast.ok("이상 없음 — 사전심사 통과"); },
    onError: fail });
  const upd = (i: number, k: keyof Line) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setLines(lines.map((l, j) => j === i ? { ...l, [k]: e.target.value } : l));
  const tone = (s: string) => s === "BLOCK" ? "red" : "amber";

  interface Diag { code: string; available: boolean; held?: boolean; risk_level?: string | null;
    risk_score?: number; recommendation?: string; note?: string;
    evidence_citations?: { title?: string; relevant_passage?: string }[] }
  const [diags, setDiags] = useState<Diag[]>([]);
  const aiEval = useMutation({
    mutationFn: async () => Promise.all(lines.filter((l) => l.code).map((l) =>
      api<Diag>("/regulatory/notices/ai-diagnose", { method: "POST", body: JSON.stringify({
        code: l.code, service_date: l.treated_on || null, context: `${l.code} ${l.count}회` }) })
        .then((d) => ({ ...d, code: l.code }))
        .catch(() => ({ code: l.code, available: false } as Diag)))),
    onSuccess: (ds) => setDiags(ds) });
  const riskTone = (r?: string | null) => r === "HIGH" ? "red" : r === "MEDIUM" ? "amber" : "gray";

  const [nq, setNq] = useState("");
  const [nquery, setNquery] = useState("");
  const notices = useQuery({ enabled: !!nquery, queryKey: ["notice-aisearch", nquery],
    queryFn: () => api<{ source: string; items: NoticeHit[] }>(
      `/regulatory/notices/ai-search?q=${encodeURIComponent(nquery)}&limit=10`) });

  const input = "rounded-md border border-slate-300 px-2 py-1 text-sm";
  const tabBtn = (k: typeof tab, label: string) => (
    <button onClick={() => setTab(k)}
      className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === k
        ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
      {label}</button>);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">의료보험 — 사전심사·심사 워크리스트</h1>
      <div className="flex gap-2">
        {tabBtn("worklist", "워크리스트")}{tabBtn("run", "실행·평가")}
        {tabBtn("stats", "통계")}{tabBtn("queries", "협업 질의")}{tabBtn("auto", "자동 처리")}{tabBtn("rules", "룰 관리")}
      </div>

      {tab === "worklist" && (
        <>
          <Card title={`심사 케이스 — ${activeMonth === "" ? "전체" : activeMonth} (${monthItems.length}건)`} action={
            <span className="text-[11px] text-slate-400">위험액(삭감 위험 청구액) 내림차순</span>}>
            {cases.isError && (
              <p className="mb-2 text-sm text-rose-600">
                목록 조회 실패 — {cases.error instanceof ApiError ? cases.error.error.message : "세션/네트워크 오류"}
              </p>
            )}
            {/* 월별(진료월) 탭 — 케이스를 월 단위로 구성 */}
            <div className="mb-2 flex flex-wrap items-center gap-1.5 border-b border-slate-100 pb-2">
              <span className="mr-1 text-[11px] font-medium text-slate-400">진료월</span>
              <button onClick={() => setMonthTab("")}
                className={`rounded-md px-2.5 py-1 text-xs font-medium ${activeMonth === ""
                  ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
                전체 ({allCases.length})</button>
              {months.map((m) => (
                <button key={m} onClick={() => setMonthTab(m)}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium ${activeMonth === m
                    ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
                  {m} ({monthCounts[m]})</button>
              ))}
            </div>
            <div className="mb-2 flex flex-wrap gap-2">
              <select value={filter.status} className={input}
                onChange={(e) => setFilter({ ...filter, status: e.target.value })}>
                <option value="">전체 상태</option>
                {Object.entries(STATUS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
              </select>
              <select value={filter.severity} className={input}
                onChange={(e) => setFilter({ ...filter, severity: e.target.value })}>
                <option value="">전체 심각도</option>
                <option value="BLOCK">BLOCK</option><option value="WARN">WARN</option>
              </select>
              <select value={filter.enc} className={input}
                onChange={(e) => setFilter({ ...filter, enc: e.target.value })}>
                <option value="">입원+외래</option>
                <option value="INPATIENT">입원</option>
                <option value="OUTPATIENT">외래</option>
                <option value="MIXED">혼합</option>
              </select>
              <input value={filter.chart} placeholder="차트번호 검색" className={input}
                onChange={(e) => setFilter({ ...filter, chart: e.target.value })} />
              <span className="flex items-center gap-1">
                <button onClick={() => setFilter({ ...filter, day: shiftDay(filter.day, -1) })}
                  title="전일"
                  className="rounded border border-slate-300 px-1.5 py-1 text-xs hover:bg-slate-100">◀</button>
                <input type="date" value={filter.day} className={input} title="진료일(적발 발생일)로 좁히기"
                  onChange={(e) => setFilter({ ...filter, day: e.target.value })} />
                <button onClick={() => setFilter({ ...filter, day: shiftDay(filter.day, 1) })}
                  title="다음일"
                  className="rounded border border-slate-300 px-1.5 py-1 text-xs hover:bg-slate-100">▶</button>
                {filter.day && (
                  <button onClick={() => setFilter({ ...filter, day: "" })}
                    className="text-xs text-slate-400 hover:text-slate-600">진료일 해제</button>
                )}
              </span>
            </div>
            <Table headers={["차트", "진료월", "구분", "상태", "심각도", "적발", "위험액", "청구액", "담당", ""]}
              empty="케이스 없음 — 실행·평가 탭에서 '평가+워크리스트 저장'으로 적재하세요"
              rows={pageItems.map((c) => [
                <code key="n" className="text-xs">{c.chart_no}</code>, c.care_month,
                c.encounter_type && ENC[c.encounter_type]
                  ? <Badge key="e" tone={ENC[c.encounter_type].tone}>{ENC[c.encounter_type].label}</Badge>
                  : <span key="e" className="text-slate-400">-</span>,
                <span key="s" className="flex items-center gap-1">
                  <Badge tone={STATUS[c.status]?.tone ?? "gray"}>{STATUS[c.status]?.label ?? c.status}</Badge>
                  {c.status === "CLEARED" && c.auto_cleared && (
                    <span className="rounded bg-green-50 px-1 text-[10px] text-green-600"
                      title="무적발(리스크0) — 자동 이상없음 처리. 되돌리려면 상태 변경.">🤖 자동</span>)}
                </span>,
                c.max_severity ? <Badge key="v" tone={tone(c.max_severity)}>{c.max_severity}</Badge> : "-",
                <span key="f" className="flex items-center gap-1 text-xs">
                  {(c.block_count ?? 0) > 0 && <Badge tone="red">BLOCK {c.block_count}</Badge>}
                  {(c.warn_count ?? 0) > 0 && <Badge tone="amber">WARN {c.warn_count}</Badge>}
                  {!c.finding_count && <span className="text-slate-400">0</span>}
                </span>,
                won(c.risk_amount), won(c.claimed_amt),
                c.assignee_id ? `#${c.assignee_id}` : <span className="text-slate-400">미배정</span>,
                <span key="o" className="flex gap-1">
                  <button onClick={() => { setSelId(c.id); setAllMode(false); setStmtId(null); }}
                    className="rounded-md bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-700">
                    검토</button>
                  <button onClick={() => { setStmtId(c.id); setAllMode(false); setSelId(null); }}
                    className="rounded-md bg-slate-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-700">
                    상세</button>
                  <button onClick={() => { setSelId(c.id); setAllMode(true); setStmtId(null); }}
                    title="검토+명세 함께 보기"
                    className="rounded-md bg-violet-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-violet-700">
                    all</button>
                </span>,
              ])} />
            {monthItems.length > PAGE_SIZE && (
              <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
                <span>
                  {monthItems.length.toLocaleString()}건 중 {(safePage - 1) * PAGE_SIZE + 1}–
                  {Math.min(safePage * PAGE_SIZE, monthItems.length)} 표시
                </span>
                <span className="flex items-center gap-2">
                  <button onClick={() => setPage(safePage - 1)} disabled={safePage <= 1}
                    className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-100 disabled:opacity-40">◀ 이전</button>
                  <span>{safePage} / {pageCount}</span>
                  <button onClick={() => setPage(safePage + 1)} disabled={safePage >= pageCount}
                    className="rounded border border-slate-300 px-2 py-1 hover:bg-slate-100 disabled:opacity-40">다음 ▶</button>
                </span>
              </div>
            )}
          </Card>

          {/* 상세(진료비 명세) — 팝업(모달)으로 띄움(목록 위쪽 행에서도 즉시 확인) */}
          {stmtId !== null && (
            <Modal size="lg" bare title="진료비 명세" onClose={() => setStmtId(null)}>
              <StatementView caseId={stmtId} onClose={() => setStmtId(null)} />
            </Modal>
          )}

          {/* 검토(심사) 팝업 — all 모드면 진료비 명세도 함께(2열) */}
          {selId !== null && (
            <Modal size="lg" bare title="심사 검토"
              onClose={() => { setSelId(null); setAllMode(false); }}>
              <div className={allMode ? "grid items-start gap-3 xl:grid-cols-2" : ""}>
                <div className="space-y-3">
          {detail.isLoading && (
            <Card title={`케이스 #${selId} 불러오는 중…`}>
              <p className="py-4 text-sm text-slate-400">상세를 불러오고 있습니다.</p>
            </Card>
          )}
          {detail.isError && (
            <Card title="케이스 상세를 불러오지 못했습니다">
              <p className="py-2 text-sm text-rose-600">
                {detail.error instanceof ApiError ? detail.error.error.message : "네트워크/세션 오류"}
                {" — 새로고침 후에도 반복되면 권한(보험심사 read) 또는 배포 직후 캐시 문제일 수 있습니다."}
              </p>
              <button onClick={() => detail.refetch()}
                className="rounded-md bg-slate-600 px-3 py-1 text-xs font-medium text-white">재시도</button>
            </Card>
          )}
          {detail.data && (
            <Card title={`케이스 #${detail.data.id} — ${detail.data.chart_no} (${detail.data.care_month})`}
              action={<button onClick={() => { setSelId(null); setAllMode(false); }} className="text-xs text-slate-400 hover:text-slate-600">닫기</button>}>
              {detail.data.adjustment_history.count > 0 && (
                <p className="mb-2 rounded-md bg-rose-50 px-3 py-1.5 text-xs text-rose-700">
                  과거 실삭감 이력: {detail.data.adjustment_history.count}건 ·
                  {" "}{won(detail.data.adjustment_history.total_amt)} — 주요 사유:{" "}
                  {detail.data.adjustment_history.top_categories.map((c) => `${c.category}(${c.count})`).join(", ")}
                </p>
              )}
              <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
                <Badge tone={STATUS[detail.data.status]?.tone ?? "gray"}>{STATUS[detail.data.status]?.label}</Badge>
                {detail.data.encounter_type && ENC[detail.data.encounter_type] && (
                  <Badge tone={ENC[detail.data.encounter_type].tone}>{ENC[detail.data.encounter_type].label}</Badge>
                )}
                <span className="text-slate-500">위험액 {won(detail.data.risk_amount)} · 적발 {detail.data.finding_count}건</span>
                <span className="ml-2 text-xs text-slate-400">상태 변경:</span>
                {(NEXT[detail.data.status] ?? []).map((s) => (
                  <Button key={s} variant="neutral" size="sm"
                    onClick={() => updateCase.mutate({ id: detail.data!.id, status: s })}>
                    {STATUS[s]?.label}</Button>
                ))}
                <button onClick={() => aiReviewMut.mutate(detail.data!.id)} disabled={aiReviewMut.isPending}
                  title="미해소 적발을 심사 보조로 일괄 진단(근거·권고 참고 · 결정은 심사자)"
                  className="ml-auto rounded-md bg-violet-600 px-2.5 py-0.5 text-xs font-medium text-white hover:bg-violet-700 disabled:opacity-50">
                  {aiReviewMut.isPending ? "보조 검토 중…" : "🧭 심사 보조 검토"}</button>
              </div>
              {/* 적발 — 레코드당 2줄(좁은 'all' 2열에서도 가독): 1줄 메타·액션 / 2줄 내용 */}
              <div className="divide-y divide-slate-100 border-y border-slate-100">
                {detail.data.findings.map((f) => (
                  <div key={f.id} className="py-1.5">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                      <Badge tone={f.resolved ? "gray" : tone(f.severity)}>
                        {f.resolved ? "해소" : f.severity}</Badge>
                      <span className={`font-medium ${f.resolved ? "text-slate-400" : "text-slate-700"}`}>
                        {f.rule_name ?? f.rule_code}</span>
                      {f.reason_code && (
                        <span className="rounded bg-indigo-50 px-1 text-[10px] font-medium text-indigo-600"
                          title="거부사유 코드(HIRA 청구오류 유형)">{f.reason_code}</span>)}
                      <code className="text-slate-500">{f.code ?? "-"}</code>
                      {f.treated_on && (
                        <span className="font-mono text-[11px] text-slate-400">
                          {String(f.treated_on).slice(0, 10)}</span>)}
                      {f.amount > 0 && <span className="text-slate-500">위험액 {won(f.amount)}</span>}
                      {f.decision !== "PENDING" && (
                        <Badge tone={f.decision === "DISMISS" ? "gray" : "amber"}>
                          {f.decision === "DISMISS" ? "오탐" : "수정필요"}</Badge>)}
                      {f.decision === "DISMISS" && f.auto_decided && (
                        <span className="rounded bg-slate-100 px-1 text-[10px] text-slate-500"
                          title="확정 오탐 패턴(인간 전건 오탐) 자동 처리 — 되돌리려면 재검토.">🤖 자동오탐</span>)}
                      {!f.resolved && f.decision === "PENDING" && (
                        <span className="ml-auto flex items-center gap-1">
                          <Button variant="warning" size="sm"
                            onClick={() => decide.mutate({ id: f.id, decision: "ACCEPT" })}>인정</Button>
                          {/* 의료법 — 의학판단 적발은 단독 오탐 불가, 의료진 질의·확인 경유 */}
                          {f.physician_gated
                            ? <span className="rounded bg-violet-50 px-2 py-0.5 text-[11px] text-violet-700"
                                title="의학판단 적발 — 단독 오탐 불가. 명세에서 의료진 질의 후 의료진 확인으로 종결.">
                                🩺 의료진 확인 필요</span>
                            : <Button variant="neutral" size="sm"
                                onClick={() => setDismiss({ id: f.id, note: "" })}>오탐</Button>}
                        </span>)}
                    </div>
                    {dismiss?.id === f.id && (
                      <div className="mt-1 rounded-md border border-slate-200 bg-slate-50 p-2">
                        <textarea autoFocus value={dismiss.note} rows={2}
                          onChange={(e) => setDismiss({ id: f.id, note: e.target.value })}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitDismiss();
                            if (e.key === "Escape") setDismiss(null);
                          }}
                          placeholder="오탐 사유 (필수 — 룰 보정 데이터)"
                          className="w-full resize-y rounded border border-slate-300 px-2 py-1 text-xs" />
                        <div className="mt-1 flex items-center gap-1">
                          <Button variant="neutral" size="sm" onClick={submitDismiss}
                            disabled={!dismiss.note.trim()}>오탐 확정</Button>
                          <Button variant="secondary" size="sm"
                            onClick={() => setDismiss(null)}>취소</Button>
                          <span className="ml-auto text-[10px] text-slate-400">⌘/Ctrl+Enter 확정 · Esc 취소</span>
                        </div>
                      </div>
                    )}
                    <div className={`mt-0.5 text-xs ${f.resolved ? "text-slate-400 line-through" : "text-slate-600"}`}>
                      {f.detail}{f.decision_note ? ` — [${f.decision === "DISMISS" ? "오탐" : "인정"}] ${f.decision_note}` : ""}</div>
                    {aiReview[f.id] && (
                      <div className="mt-0.5 rounded bg-violet-50 px-2 py-1 text-[11px] text-violet-800">
                        🧭 {!aiReview[f.id].available ? (aiReview[f.id].note ?? "보조 평가 불가")
                          : aiReview[f.id].held ? (aiReview[f.id].note ?? "판단 보류 (근거 고시 없음)")
                          : <>보조 위험도 <b>{aiReview[f.id].risk_level ?? "-"}</b>
                              {aiReview[f.id].recommendation ? ` · ${aiReview[f.id].recommendation}` : ""}
                              {(aiReview[f.id].evidence_citations ?? []).some((c) => c.title) && (
                                <span className="text-violet-500"> · 근거: {(aiReview[f.id].evidence_citations ?? [])
                                  .map((c) => c.title).filter(Boolean).join(" / ")}</span>)}</>}
                      </div>
                    )}
                    {aiReview[f.id]?.false_positive_candidate && !f.resolved && f.decision === "PENDING" && (
                      <div className="mt-0.5 flex items-start gap-2 rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-800">
                        <span>⚠️ <b>오탐 후보</b> · {aiReview[f.id].dismiss_reason_draft}</span>
                        <button onClick={() => setDismiss({ id: f.id,
                            note: aiReview[f.id].dismiss_reason_draft ?? "" })}
                          className="ml-auto shrink-0 rounded bg-amber-500 px-2 py-0.5 font-medium text-white hover:bg-amber-600">
                          오탐 처리(초안)</button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
              <CaseSheetView chartNo={detail.data.chart_no} findings={detail.data.findings} />
              <div className="mt-3 space-y-1">
                {detail.data.comments.map((m) => (
                  <p key={m.id} className="text-xs text-slate-600">
                    <span className="text-slate-400">#{m.author} · {String(m.created_at).slice(0, 16)}</span> {m.body}</p>
                ))}
                <div className="flex gap-2 pt-1">
                  <input value={memo} onChange={(e) => setMemo(e.target.value)} placeholder="검토 메모"
                    className={`${input} w-96`} />
                  <Button variant="neutral" size="sm" disabled={!memo.trim() || comment.isPending}
                    onClick={() => comment.mutate({ id: detail.data!.id, body: memo })}>
                    기록</Button>
                </div>
              </div>
            </Card>
          )}
                </div>
                {allMode && (
                  <StatementView caseId={selId} onClose={() => { setSelId(null); setAllMode(false); }} />
                )}
              </div>
            </Modal>
          )}

          <BulkSheetCard onApplied={refresh} items={monthItems}
            filterLabel={filterLabel} exportQuery={exportQuery} />
        </>
      )}

      {tab === "stats" && <StatsTab />}
      {tab === "queries" && (
        <QueriesTab onOpenCase={(id) => {
          setTab("worklist"); setStmtId(id); setAllMode(false); setSelId(null); }} />
      )}
      {tab === "auto" && (
        <AutoActionsTab onOpenCase={(id) => {
          setTab("worklist"); setSelId(id); setAllMode(false); setStmtId(null); }} />
      )}
      {tab === "rules" && <RulesTab />}

      {tab === "run" && (
        <>
          <SheetInputCard busy={run.isPending}
            onEvaluate={(apiLines, save) => run.mutate({ save, apiLines })} />
          <Card title="청구 라인 입력" action={
            <Button variant="secondary" size="sm"
              onClick={() => setLines([...lines, { ...EMPTY }])}>+ 라인</Button>}>
            <div className="space-y-1">
              {lines.map((l, i) => (
                <div key={i} className="grid grid-cols-5 gap-1">
                  <input placeholder="차트번호" value={l.chart_no} onChange={upd(i, "chart_no")} className={input} />
                  <input placeholder="수가코드" value={l.code} onChange={upd(i, "code")} className={input} />
                  <input type="number" placeholder="횟수" value={l.count} onChange={upd(i, "count")} className={input} />
                  <input type="number" placeholder="청구액" value={l.amount} onChange={upd(i, "amount")} className={input} />
                  <input type="date" value={l.treated_on} onChange={upd(i, "treated_on")} className={input} />
                </div>
              ))}
            </div>
            <div className="mt-2 flex gap-2">
              <Button onClick={() => run.mutate({ save: false })} isLoading={run.isPending}>
                사전심사 실행(룰)</Button>
              <Button variant="success" onClick={() => run.mutate({ save: true })}
                isLoading={run.isPending}>평가 + 워크리스트 저장</Button>
              {/* 심사 보조(AI)는 보라 브랜드색 유지 — 전역 토큰에 없는 의도적 구분 */}
              <button onClick={() => aiEval.mutate()} disabled={aiEval.isPending}
                className="rounded bg-violet-600 px-4 py-1 text-sm text-white disabled:opacity-50">
                {aiEval.isPending ? "보조 평가 중…" : "보조 삭감위험 평가"}</button>
            </div>
          </Card>

          {diags.length > 0 && (
            <Card title="심사 보조 삭감위험 평가 (weve · 휴먼인더루프)" action={
              <span className="text-[11px] text-slate-400">보조·법적효력 없음·근거 인용 시만 표시</span>}>
              <Table headers={["코드", "위험도", "점수", "권고", "근거 고시"]}
                rows={diags.map((d) => [
                  <code key="c" className="text-xs">{d.code}</code>,
                  !d.available ? <span className="text-xs text-slate-400">미연결</span>
                    : d.held ? <Badge tone="gray">판단 보류</Badge>
                    : <Badge tone={riskTone(d.risk_level)}>{d.risk_level}</Badge>,
                  d.risk_score != null ? d.risk_score.toFixed(2) : "-",
                  <span key="r" className="text-xs">{d.recommendation ?? d.note ?? "-"}</span>,
                  <span key="e" className="text-xs text-slate-500">
                    {(d.evidence_citations ?? []).map((c) => c.title).filter(Boolean).join(" · ") || "-"}</span>,
                ])} />
              <p className="mt-2 text-[11px] text-slate-400">
                근거 고시가 없으면 ‘판단 보류’(환각 방지). 최종 청구 결정은 담당자(휴먼인더루프).
              </p>
            </Card>
          )}

          {result && (
            <Card title={`사전심사 결과 — ${result.count}건 ${result.blocked ? "(제출 차단)" : ""}`}>
              {result.count === 0
                ? <Badge tone="green">이상 없음</Badge>
                : <Table headers={["심각도", "룰", "차트", "코드", "내용"]}
                    rows={result.findings.map((f) => [
                      <Badge key="s" tone={tone(f.severity)}>{f.severity}</Badge>,
                      f.rule_name, f.chart_no, f.code, f.detail])} />}
            </Card>
          )}

          <Card title="관련 공시·심사기준 검색 (근거 retrieval)" action={
            <span className="text-[11px] text-slate-400">
              {notices.data?.source === "weve" ? "weve 색인" : "로컬 검색"}</span>}>
            <div className="flex items-center gap-2">
              <input value={nq} onChange={(e) => setNq(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && setNquery(nq)}
                placeholder="수가코드·상병·키워드 (예: 중복 산정)" className={`${input} w-72`} />
              <Button variant="neutral" onClick={() => setNquery(nq)}>검색</Button>
            </div>
            {nquery && (
              <div className="mt-3 space-y-2">
                {(notices.data?.items ?? []).length === 0 && (
                  <p className="text-sm text-slate-400">일치하는 공시 없음</p>)}
                {(notices.data?.items ?? []).map((n) => (
                  <div key={n.erp_ref_id} className="rounded-md border border-slate-200 p-2">
                    <div className="flex items-center gap-2">
                      <Badge tone="blue">{n.notice_type}</Badge>
                      <span className="text-sm font-medium text-slate-800">{n.title}</span>
                      <span className="text-[11px] text-slate-400">시행 {n.effective_from} · 적합도 {n.score}</span>
                    </div>
                    <p className="mt-1 text-xs text-slate-600">{n.snippet}</p>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
