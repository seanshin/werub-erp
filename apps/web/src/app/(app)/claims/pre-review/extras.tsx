"use client";
/** 사전심사 PR-B 보조 컴포넌트 — 시트 입력/반출·통계(Recharts)·룰 관리.
 *  시트는 작업면, 정본은 케이스 DB (claims-worklist plan §4). */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Sheet } from "@fortune-sheet/core";
import { api, ApiError, apiDownload } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import SheetChart, { type Grid } from "@/components/SheetChart";
import Spreadsheet, { sheetToGrid, toSheet } from "@/components/Spreadsheet";

export interface ApiLine { chart_no: string; code: string; count: number;
  amount: number | null; treated_on: string | null; age?: number | null; sex?: string | null }

const SHEET_HEADERS = ["차트번호", "수가코드", "횟수", "청구액", "진료일", "연령", "성별"];
const num = (v: string | number | null): number | null => {
  if (v === null || v === "") return null;
  const n = typeof v === "number" ? v : Number(String(v).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
};

/** 시트 grid → 평가 라인 변환 — 1행 헤더 가정, 차트번호·코드 없는 행 스킵. */
export function gridToLines(grid: Grid): { lines: ApiLine[]; skipped: number } {
  const lines: ApiLine[] = [];
  let skipped = 0;
  for (let r = 1; r < grid.length; r++) {
    const row = grid[r] ?? [];
    const chart = String(row[0] ?? "").trim();
    const code = String(row[1] ?? "").trim();
    if (!chart && !code) continue;                  // 빈 행
    if (!chart || !code) { skipped++; continue; }
    lines.push({
      chart_no: chart, code, count: num(row[2] ?? null) ?? 1,
      amount: num(row[3] ?? null),
      treated_on: row[4] ? String(row[4]).slice(0, 10) : null,
      age: num(row[5] ?? null), sex: row[6] ? String(row[6]).trim().toUpperCase() : null });
  }
  return { lines, skipped };
}

/** 실행 탭 — 시트 붙여넣기 평가 (HIS 추출 엑셀 복붙 워크플로). */
export function SheetInputCard({ onEvaluate, busy }: {
  onEvaluate: (lines: ApiLine[], save: boolean) => void; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const template = useMemo<Sheet[]>(() => [toSheet("청구라인", SHEET_HEADERS, [])], []);
  const gridRef = useRef<Grid>([]);
  const keyRef = useRef("");
  const [parsed, setParsed] = useState<{ lines: number; skipped: number } | null>(null);
  const ask = useConfirm();

  const onChange = useCallback((d: Sheet[]) => {
    const g = sheetToGrid(d[0]);
    const key = JSON.stringify(g);
    if (key === keyRef.current) return;             // FortuneSheet 재발화 가드 (React #185)
    keyRef.current = key;
    gridRef.current = g;
    const { lines, skipped } = gridToLines(g);
    setParsed({ lines: lines.length, skipped });
  }, []);

  const evaluate = async (save: boolean) => {
    const { lines, skipped } = gridToLines(gridRef.current);
    if (!lines.length) return;
    if (skipped > 0 && !(await ask({ title: "일부 행 제외",
        body: `${skipped}개 행은 차트번호/코드 누락으로 제외됩니다. 계속할까요?`, confirmLabel: "계속" }))) return;
    onEvaluate(lines, save);
  };

  return (
    <Card title="시트 붙여넣기 평가" action={
      <button onClick={() => setOpen(!open)} className="text-xs text-blue-600 hover:underline">
        {open ? "접기" : "열기 — HIS 추출 엑셀을 그대로 붙여넣기"}</button>}>
      {open && (
        <div className="space-y-2">
          <p className="text-xs text-slate-500">
            1행 헤더({SHEET_HEADERS.join("·")}) 아래에 데이터를 붙여넣으세요. 연령·성별은 선택(연령/성별 제한 룰).
          </p>
          <Spreadsheet data={template} height={300} onChange={onChange} />
          <div className="flex items-center gap-2">
            <Button isLoading={busy} disabled={!parsed?.lines}
              onClick={() => evaluate(false)}>시트에서 평가</Button>
            <Button variant="success" isLoading={busy} disabled={!parsed?.lines}
              onClick={() => evaluate(true)}>시트에서 평가 + 워크리스트 저장</Button>
            {parsed && <span className="text-xs text-slate-400">
              인식 {parsed.lines}행{parsed.skipped ? ` · 제외 ${parsed.skipped}행` : ""}</span>}
          </div>
        </div>
      )}
    </Card>
  );
}

interface CaseFinding { severity: string; code: string | null; rule_name: string | null;
  detail: string | null; amount: number; decision: string; resolved: boolean }

/** 케이스 상세 — 적발 내역 시트 뷰 (BLOCK=적색·WARN=황색 하이라이트, xlsx 반출은 서버). */
export function CaseSheetView({ chartNo, findings }: { chartNo: string; findings: CaseFinding[] }) {
  const [open, setOpen] = useState(false);
  const sheets = useMemo<Sheet[]>(() => {
    const rows = findings.map((f) => [
      chartNo, f.code ?? "", f.rule_name ?? "", f.severity, f.amount,
      f.resolved ? "해소" : f.decision, f.detail ?? ""]);
    return [toSheet("적발내역", ["차트", "코드", "룰", "심각도", "관련액", "결정", "내용"], rows,
      (ri) => findings[ri]?.resolved ? "#e2e8f0"
        : findings[ri]?.severity === "BLOCK" ? "#fecaca" : "#fde68a")];
  }, [chartNo, findings]);
  return (
    <div className="mt-2">
      <button onClick={() => setOpen(!open)} className="text-xs text-blue-600 hover:underline">
        {open ? "시트 닫기" : "시트로 보기 (하이라이트)"}</button>
      {open && <div className="mt-2"><Spreadsheet data={sheets} height={260} /></div>}
    </div>
  );
}

/** 워크리스트 — xlsx 반출 + 시트 일괄 상태반영(왕복). */
export function BulkSheetCard({ onApplied, items, filterLabel, exportQuery }: {
  onApplied: () => void;
  items: { chart_no: string; care_month: string; status: string;
    encounter_type?: string | null; max_severity?: string | null; finding_count?: number;
    block_count?: number; warn_count?: number; risk_amount?: number; claimed_amt?: number }[];
  filterLabel: string;
  exportQuery: string;
}) {
  const [open, setOpen] = useState(false);
  const toast = useToast();
  const gridRef = useRef<Grid>([]);
  const keyRef = useRef("");
  const [sheets, setSheets] = useState<Sheet[] | null>(null);
  const [big, setBig] = useState(false);                 // 큰 창(리사이즈 가능)
  const panelRef = useRef<HTMLDivElement>(null);
  const [sheetH, setSheetH] = useState(600);

  // 워크리스트와 동일한 필터링 결과(items)로 시트 구성 — 별도 전체 로드 안 함.
  // A·B·C(차트·진료월·상태)는 일괄반영 대상, D~는 심사 평가용 참고자료(읽기).
  const load = () => {
    const enc: Record<string, string> = { OUTPATIENT: "외래", INPATIENT: "입원", MIXED: "혼합" };
    const rows = items.map((c) => [
      c.chart_no, c.care_month, c.status,
      c.encounter_type ? (enc[c.encounter_type] ?? c.encounter_type) : "",
      c.max_severity ?? "",
      [c.block_count ? `BLOCK ${c.block_count}` : null, c.warn_count ? `WARN ${c.warn_count}` : null]
        .filter(Boolean).join(" ") || String(c.finding_count ?? 0),
      c.risk_amount ?? 0, c.claimed_amt ?? 0,
    ]);
    setSheets([toSheet("워크리스트",
      ["차트번호", "진료월", "상태", "구분", "심각도", "적발", "위험액", "청구액"], rows)]);
    setOpen(true);
  };
  const openBig = () => { if (!sheets) load(); setBig(true); };
  // 패널 드래그 리사이즈 → 시트 높이 추종 + ESC 닫기
  useEffect(() => {
    if (!big || !panelRef.current) return;
    const ro = new ResizeObserver((es) => setSheetH(Math.max(300, es[0].contentRect.height - 96)));
    ro.observe(panelRef.current);
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setBig(false); };
    window.addEventListener("keydown", onKey);
    return () => { ro.disconnect(); window.removeEventListener("keydown", onKey); };
  }, [big]);
  const onChange = useCallback((d: Sheet[]) => {
    const g = sheetToGrid(d[0]);
    const key = JSON.stringify(g);
    if (key === keyRef.current) return;
    keyRef.current = key;
    gridRef.current = g;
  }, []);
  const apply = useMutation({
    mutationFn: () => {
      const items = (gridRef.current.slice(1) ?? []).filter((r) => r[0]).map((r) => ({
        chart_no: String(r[0]), care_month: String(r[1] ?? ""), status: String(r[2] ?? "") }));
      return api<{ applied: number; skipped: { chart_no: string; reason: string }[] }>(
        "/claims/pre-review/cases/bulk-status", { method: "POST",
          body: JSON.stringify({ items }) });
    },
    onSuccess: (r) => { toast.ok(`반영 ${r.applied}건${r.skipped.length
      ? ` · 스킵 ${r.skipped.length}건 (${r.skipped.slice(0, 3).map((s) => `${s.chart_no}:${s.reason}`).join(" / ")}${r.skipped.length > 3 ? "…" : ""})` : ""}`);
      onApplied(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "반영 실패"),
  });

  return (
    <Card title={`시트 일괄 작업 (${items.length}건)`} action={
      <span className="text-[11px] text-slate-400">상태 열 수정 후 일괄 반영 — 전이규칙 위반은 스킵</span>}>
      <p className="mb-2 text-xs text-slate-500">
        <span className="font-medium text-slate-600">필터:</span> {filterLabel} — 위 워크리스트와 동일 대상
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={load}
          className="rounded-md bg-slate-700 px-3 py-1.5 text-sm text-white">시트 편집 모드</button>
        <button onClick={openBig}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm text-white">⤢ 큰 창에서 편집</button>
        <button onClick={() => apiDownload(
          `/claims/pre-review/cases-export.xlsx${exportQuery ? `?${exportQuery}` : ""}`,
          "pre-review-worklist.xlsx")}
          className="rounded-md bg-slate-500 px-3 py-1.5 text-sm text-white">xlsx 반출</button>
        {open && <Button variant="success" isLoading={apply.isPending}
          onClick={() => apply.mutate()}>일괄 반영</Button>}
      </div>
      {open && !big && sheets && (
        <div className="mt-2">
          <p className="mb-1 text-xs text-slate-400">
            상태(C열) 값: OPEN·IN_REVIEW·CLEARED·FIX_REQUESTED·BLOCKED·RESOLVED — 일괄반영은 상태만,
            구분·심각도·적발·위험액·청구액은 심사 평가 참고(읽기)</p>
          <Spreadsheet data={sheets} height={300} onChange={onChange} />
        </div>
      )}

      {/* 큰 창 — 리사이즈 가능(우하단 드래그)·ESC·오버레이 클릭 닫기 */}
      {big && sheets && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
          onClick={() => setBig(false)}>
          <div ref={panelRef} onClick={(e) => e.stopPropagation()}
            style={{ resize: "both", overflow: "auto", width: "92vw", height: "86vh",
                     minWidth: 560, minHeight: 360 }}
            className="flex flex-col rounded-lg bg-white shadow-2xl">
            <div className="flex items-center justify-between border-b border-slate-200 px-4 py-2">
              <span className="text-sm font-semibold text-slate-700">
                시트 일괄 작업 — {filterLabel} ({items.length}건)</span>
              <div className="flex items-center gap-2">
                <Button variant="success" isLoading={apply.isPending}
                  onClick={() => apply.mutate()}>일괄 반영</Button>
                <Button variant="secondary" onClick={() => setBig(false)}>닫기</Button>
              </div>
            </div>
            <p className="px-4 pt-1 text-xs text-slate-400">
              상태(C열): OPEN·IN_REVIEW·CLEARED·FIX_REQUESTED·BLOCKED·RESOLVED — 일괄반영은 상태만,
              구분·심각도·적발·위험액·청구액은 평가 참고 · 우하단 모서리를 끌어 크기 조절</p>
            <div className="flex-1 overflow-hidden p-2">
              <Spreadsheet data={sheets} height={sheetH} onChange={onChange} />
            </div>
          </div>
        </div>
      )}
    </Card>
  );
}

/** 통계 탭 — 룰별 적발·오탐(차트+시트 데이터), 상태 분포·위험액. */
export function StatsTab() {
  const stats = useQuery({ queryKey: ["pr-stats"], queryFn: () =>
    api<{ by_rule: { rule_code: string; rule_name: string | null; findings: number;
      dismissed: number; accepted: number; dismiss_rate: number }[];
      by_status: { status: string; count: number; risk_amount: number }[];
      by_reason_group?: { group: string; label: string; findings: number; amount: number }[] }>(
      "/claims/pre-review/stats") });

  const triage = useQuery({ queryKey: ["pr-triage"], queryFn: () =>
    api<{ items: { case_id: number; chart_no: string; care_month: string; status: string;
      risk_amount: number; predicted_reduction: number; findings: number;
      max_severity: string | null }[] }>("/claims/pre-review/triage?limit=20") });

  const ruleGrid = useMemo<Grid>(() => {
    const rows = (stats.data?.by_rule ?? []).map((r) =>
      [r.rule_name ?? r.rule_code, r.findings, r.dismissed, r.accepted]);
    return [["룰", "적발", "오탐", "인정"], ...rows] as Grid;
  }, [stats.data]);
  const statusGrid = useMemo<Grid>(() => {
    const rows = (stats.data?.by_status ?? []).map((s) => [s.status, s.count, s.risk_amount]);
    return [["상태", "건수", "위험액"], ...rows] as Grid;
  }, [stats.data]);

  return (
    <div className="space-y-4">
      <Card title="룰별 적발·오탐 (오탐률 = 룰 보정 근거)">
        <SheetChart grid={ruleGrid} />
        <Table headers={["룰", "적발", "오탐(DISMISS)", "인정(ACCEPT)", "오탐률"]}
          rows={(stats.data?.by_rule ?? []).map((r) => [
            r.rule_name ?? r.rule_code, r.findings, r.dismissed, r.accepted,
            <span key="d" className={r.dismiss_rate >= 0.5 ? "font-semibold text-rose-600" : ""}>
              {(r.dismiss_rate * 100).toFixed(0)}%</span>])} />
      </Card>
      <Card title="상태 분포·위험액">
        <SheetChart grid={statusGrid} />
      </Card>
      <Card title="삭감위험 우선순위 (P2 triage — 확정 인정률 기반·참고)">
        <Table headers={["차트", "진료월", "상태", "예측삭감", "위험액", "적발"]}
          rows={(triage.data?.items ?? []).map((t) => [
            t.chart_no, t.care_month, t.status,
            won(t.predicted_reduction),
            won(t.risk_amount), t.findings])}
          empty="미결 케이스 없음" />
        <p className="mt-1 text-[11px] text-slate-400">
          예측삭감 = Σ(미해소 적발액 × 패턴별 확정 인정률). 우선순위 참고용(자동 결정 아님).</p>
      </Card>
      <Card title="거부사유 대분류 분포 (HIRA 청구오류 유형)">
        <Table headers={["대분류", "사유", "적발", "위험액"]}
          rows={(stats.data?.by_reason_group ?? []).map((r) => [
            r.group, r.label, r.findings, won(r.amount)])}
          empty="집계 없음 (재실행 시 사유코드 부여)" />
      </Card>
    </div>
  );
}

/** 룰 관리 탭 — on/off·심각도·params (변경=ADMIN, 감사기록). */
export function RulesTab() {
  const qc = useQueryClient();
  const { ask, dialog } = useInputDialog();
  const toast = useToast();
  const rules = useQuery({ queryKey: ["pr-rules"], queryFn: () =>
    api<{ items: { id: number; code: string; name: string; rule_type: string;
      params: Record<string, unknown> | null; severity: string; active: boolean;
      // 시점유효(G7) — 실행 EP는 진료일 as_of로 룰을 거르는데 화면은 "지금 켜져 있음"만 보여줘
      // "이 진료월엔 적용 안 되는 룰"을 구분할 수 없었다. 의료진 게이트도 상세에만 있었다.
      physician_gated: boolean; valid_from: string | null; valid_to: string | null }[] }>(
      "/claims/pre-review/rules") });
  const stats = useQuery({ queryKey: ["pr-stats"], queryFn: () =>
    api<{ by_rule: { rule_code: string; dismiss_rate: number; findings: number }[] }>(
      "/claims/pre-review/stats") });
  const ruleStat = (code: string) => stats.data?.by_rule.find((r) => r.rule_code === code);

  const save = useMutation({
    mutationFn: (p: { id: number; body: Record<string, unknown> }) =>
      api(`/claims/pre-review/rules/${p.id}`, { method: "PUT", body: JSON.stringify(p.body) }),
    onSuccess: () => { toast.ok("룰 변경 완료 (감사기록)"); qc.invalidateQueries({ queryKey: ["pr-rules"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "변경 실패 (ADMIN 전용)"),
  });

  return (
    <Card title="사전심사 룰 관리" action={
      <span className="text-[11px] text-slate-400">변경은 ADMIN · 오탐률을 보정 근거로</span>}>
      {dialog}
      <Table headers={["코드", "이름", "타입", "효력기간", "params", "심각도", "오탐률",
        "활성", ""]}
        rows={(rules.data?.items ?? []).map((r) => [
          <code key="c" className="text-xs">{r.code}</code>,
          <span key="n" className="flex items-center gap-1">
            {r.name}
            {/* 의료진 확인 게이트 대상 룰인지 — 룰 관리에서 안 보여 게이트 룰인 줄 모르고
                심각도·활성을 바꾸던 문제(의료법 게이트) */}
            {r.physician_gated && <Badge tone="blue">의료진 게이트</Badge>}
          </span>,
          r.rule_type,
          <span key="v" className="whitespace-nowrap text-xs text-slate-500">
            {r.valid_from ?? "—"} ~ {r.valid_to ?? "현행"}</span>,
          <code key="p" className="text-[10px] text-slate-500">{JSON.stringify(r.params)}</code>,
          <Badge key="s" tone={r.severity === "BLOCK" ? "red" : "amber"}>{r.severity}</Badge>,
          (() => {                              // 표본 인지형 보정 권고 — A3 오탐 데이터 → 룰 액션
            const st = ruleStat(r.code);
            if (!st || st.findings === 0) return "-";
            const flag = st.findings >= 5 && st.dismiss_rate >= 0.5;   // 충분표본·과다오탐
            return <span key="dr" className={flag ? "font-semibold text-rose-600" : ""}>
              {(st.dismiss_rate * 100).toFixed(0)}%
              <span className="ml-0.5 text-[10px] text-slate-400">(n{st.findings})</span>
              {flag && r.active && (
                <span className="ml-1 rounded bg-rose-100 px-1 text-[10px] text-rose-700"
                  title="오탐률 50%↑·표본 5건↑ — 룰 완화(WARN)·params 조정·비활성 검토">⚠완화검토</span>)}
            </span>;
          })(),
          r.active ? <Badge key="a" tone="green">ON</Badge> : <Badge key="a" tone="gray">OFF</Badge>,
          <span key="b" className="flex gap-1">
            <button onClick={() => save.mutate({ id: r.id, body: { active: !r.active } })}
              className="rounded bg-slate-600 px-2 py-0.5 text-xs text-white">
              {r.active ? "끄기" : "켜기"}</button>
            <button onClick={async () => { const v = await ask({ title: "룰 파라미터 (JSON)",
              label: "params JSON", required: true, defaultValue: JSON.stringify(r.params ?? {}),
              validate: (s) => { try { JSON.parse(s); return null; } catch { return "JSON 형식 오류"; } } });
              if (typeof v === "string" && v) save.mutate({ id: r.id, body: { params: JSON.parse(v) } }); }}
              className="rounded bg-slate-500 px-2 py-0.5 text-xs text-white">params</button>
            <button onClick={() => save.mutate({ id: r.id,
              body: { severity: r.severity === "BLOCK" ? "WARN" : "BLOCK" } })}
              className="rounded bg-slate-400 px-2 py-0.5 text-xs text-white">심각도</button>
          </span>,
        ])} />
    </Card>
  );
}


// ── 진료비 명세 (P1b) — 라인별 계산 분해·본인부담 구성·codeType 그룹·적발 연결 ──
interface StmtFinding { id: number; rule_name: string; severity: string;
  detail: string; amount: number; decision: string; resolved: boolean }
interface StmtLine { code: string; name: string | null; code_type: string;
  drug_name: string | null; drug_ingredient: string | null;
  category: string; benefit_label?: string | null; time_surcharge: string | null;
  treated_on: string | null; treated_at: string | null;
  fee_effective_from: string | null; fee_valid_to: string | null; fee_source: string | null;
  count: number; unit_price: number; price_source: string; priced: boolean;
  coverage: string; total: number; patient: number; insurer: number;
  non_covered: number; rate: number; rate_basis: string; formula: string;
  age?: number | null; sex?: string | null; admitted_on?: string | null;
  special_case_type?: string | null; medical_aid_class?: string | null;
  findings: StmtFinding[] }
interface StmtGroup { category: string; label: string; claimed: number;
  patient: number; lines: StmtLine[] }
interface Reply { id: number; reply_type: string; body: string; author: number; created_at: string }
interface Query { id: number; case_id: number; finding_id: number | null;
  line_code: string | null; status: string; question: string; raised_by: number;
  assigned_to: number | null; resolution_note: string | null; created_at: string;
  replies: Reply[] }
interface Statement { case_id: number; chart_no: string; care_month: string;
  encounter_type: string | null; available: boolean;
  summary: { total: number; patient_share: number; insurer_share: number;
    non_covered: number; prepaid: number; balance_due: number };
  groups: StmtGroup[]; warnings: string[]; queries: Query[];
  benefit_summary?: { category: string; label: string; claimed: number; patient: number }[];
  institution_grade: string | null; diagnosis_codes?: string[] }

// 임상영역 아이콘 — 심사 스캔 가독성(진단/처치·수술/검사/영상/투약…)
const CLIN_ICON: Record<string, string> = {
  CONSULT: "🩺", PROC: "🔧", LAB: "🔬", IMAGING: "📷", INJECT: "💉",
  ANES: "💤", PT: "🧎", DRUG: "💊", MATERIAL: "🧰", ADMISSION: "🛏️",
  MEAL: "🍚", NONCOVERED: "⚪", ETC: "▫️" };

const SRC: Record<string, string> = { MASTER: "ERP마스터", HIS: "HIS산정가", NONE: "미해소" };
const QSTATUS: Record<string, { label: string; tone: "gray" | "blue" | "amber" | "green" }> = {
  OPEN: { label: "질의중", tone: "amber" }, ANSWERED: { label: "회신됨", tone: "blue" },
  ACCEPTED: { label: "근거인정", tone: "green" }, REVISED: { label: "수정완료", tone: "green" } };

type Composer = { kind: "ask" | "evidence" | "revise" | "resolve"; id: number;
  line_code?: string; finding_id?: number };

export function StatementView({ caseId, onClose }: { caseId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [composer, setComposer] = useState<Composer | null>(null);   // 인라인 작성(브라우저 prompt 대체)
  const [draft, setDraft] = useState("");
  const [assignee, setAssignee] = useState("");                      // 질의 담당 의료진 사번(선택)
  const openComposer = (c: Composer) => { setComposer(c); setDraft(""); setAssignee(""); };
  const closeComposer = () => { setComposer(null); setDraft(""); setAssignee(""); };
  const stmt = useQuery({ queryKey: ["case-stmt", caseId],
    queryFn: () => api<Statement>(`/claims/pre-review/cases/${caseId}/statement`) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["case-stmt", caseId] });
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  // 공시 근거 — 라인별 삭감위험 평가(근거 인용) + 자유 공시 검색
  interface Diag { code: string; available: boolean; risk_level?: string | null;
    risk_score?: number; held?: boolean; recommendation?: string; note?: string;
    reasoning?: string; disclaimer?: string; samples?: number; attempted?: number;
    evidence_samples?: number; degraded?: boolean; risk_votes?: Record<string, number>;
    evidence_citations?: { title?: string; effective_from?: string; relevant_passage?: string;
      notice_type?: string; score?: number }[] }
  const [diag, setDiag] = useState<Diag | null>(null);
  const aiDiag = useMutation({
    mutationFn: (p: { code: string; context: string }) =>
      api<Diag>("/regulatory/notices/ai-diagnose", { method: "POST",
        body: JSON.stringify({ code: p.code, context: p.context }) }),
    onSuccess: (d, v) => setDiag({ ...d, code: v.code }),
    onError: fail });
  interface NoticeHit { title: string; notice_type: string; score: number;
    snippet: string; effective_from: string }
  const [nq, setNq] = useState("");
  const [nquery, setNquery] = useState("");
  const notices = useQuery({ enabled: !!nquery, queryKey: ["stmt-notice", nquery],
    queryFn: () => api<{ source: string; items: NoticeHit[] }>(
      `/regulatory/notices/ai-search?q=${encodeURIComponent(nquery)}&limit=8`) });

  const ask = useMutation({
    mutationFn: (p: { question: string; finding_id?: number; line_code?: string;
      assigned_to?: number }) =>
      api(`/claims/pre-review/cases/${caseId}/queries`, { method: "POST",
        body: JSON.stringify(p) }),
    onSuccess: () => { toast.ok("의료진 질의 생성"); refresh(); }, onError: fail });
  const reply = useMutation({
    mutationFn: (p: { id: number; body: string; reply_type: string }) =>
      api(`/claims/pre-review/queries/${p.id}/replies`, { method: "POST",
        body: JSON.stringify({ body: p.body, reply_type: p.reply_type }) }),
    onSuccess: () => { toast.ok("회신 기록"); refresh(); }, onError: fail });
  const resolve = useMutation({
    mutationFn: (p: { id: number; status: string; note?: string; confirmed_by_employee?: number }) =>
      api(`/claims/pre-review/queries/${p.id}`, { method: "PATCH",
        body: JSON.stringify({ status: p.status, note: p.note,
          confirmed_by_employee: p.confirmed_by_employee }) }),
    onSuccess: () => { toast.ok("질의 종결"); refresh(); }, onError: fail });
  const submitComposer = () => {
    const text = draft.trim();
    if (!composer || !text) return;
    if (composer.kind === "ask")
      ask.mutate({ question: text, line_code: composer.line_code, finding_id: composer.finding_id,
        assigned_to: assignee ? Number(assignee) : undefined });
    else if (composer.kind === "evidence")
      reply.mutate({ id: composer.id, body: text, reply_type: "EVIDENCE" });
    else if (composer.kind === "revise")
      reply.mutate({ id: composer.id, body: text, reply_type: "REVISE" });
    else if (composer.kind === "resolve")
      resolve.mutate({ id: composer.id, status: "ACCEPTED", note: text,
        confirmed_by_employee: assignee ? Number(assignee) : undefined });
    closeComposer();
  };
  // 인라인 작성 패널 — 함수가 JSX를 직접 반환(중첩 컴포넌트 아님 → textarea 포커스 유지).
  // Tailwind purge 안전: 완전한 클래스 문자열만 사용.
  const ACCENT = {
    indigo: { box: "border-indigo-200 bg-indigo-50/40", btn: "bg-indigo-600 hover:bg-indigo-700" },
    blue: { box: "border-blue-200 bg-blue-50/40", btn: "bg-blue-600 hover:bg-blue-700" },
    amber: { box: "border-amber-200 bg-amber-50/40", btn: "bg-amber-600 hover:bg-amber-700" },
    green: { box: "border-green-200 bg-green-50/40", btn: "bg-green-600 hover:bg-green-700" },
  } as const;
  const renderComposer = (placeholder: string, accent: keyof typeof ACCENT = "indigo",
                          withAssignee = false) => (
    <div className={`mt-1 rounded-md border p-2 ${ACCENT[accent].box}`}>
      {withAssignee && (
        <input value={assignee} inputMode="numeric"
          onChange={(e) => setAssignee(e.target.value.replace(/[^0-9]/g, ""))}
          placeholder="의료진 사번 — 질의 담당 / 종결 시 확인 책임의(의학판단 적발 필수)"
          className="mb-1 w-80 rounded border border-slate-300 px-2 py-1 text-xs" />
      )}
      <textarea autoFocus value={draft} onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submitComposer();
          if (e.key === "Escape") closeComposer();
        }}
        rows={2} placeholder={placeholder}
        className="w-full resize-y rounded border border-slate-300 px-2 py-1 text-sm" />
      <div className="mt-1 flex items-center gap-1">
        <button onClick={submitComposer} disabled={!draft.trim()}
          className={`rounded px-2.5 py-0.5 text-xs font-medium text-white disabled:opacity-40 ${ACCENT[accent].btn}`}>
          보내기</button>
        <Button variant="secondary" size="sm" onClick={closeComposer}>취소</Button>
        <span className="ml-auto text-[10px] text-slate-400">⌘/Ctrl+Enter 전송 · Esc 취소</span>
      </div>
    </div>
  );

  if (stmt.isLoading) return <Card title="진료비 명세 불러오는 중…"><p className="py-3 text-sm text-slate-400">계산 분해 중…</p></Card>;
  if (stmt.isError || !stmt.data) return (
    <Card title="진료비 명세를 불러오지 못했습니다">
      <p className="py-2 text-sm text-rose-600">
        {stmt.error instanceof ApiError ? stmt.error.error.message : "네트워크/세션 오류"}</p>
      <Button variant="neutral" size="sm" onClick={() => stmt.refetch()}>재시도</Button>
    </Card>);

  const d = stmt.data;
  const s = d.summary;
  // 임상 컨텍스트(수신분) — 연령·성별 제한 수가·자격 심사 보조
  const allLines = d.groups.flatMap((g) => g.lines);
  const demo = allLines.find((l) => l.age != null || l.sex);
  const uniq = (xs: (string | null | undefined)[]) =>
    [...new Set(xs.filter((x): x is string => !!x))];
  const specials = uniq(allLines.map((l) => l.special_case_type));
  const aidClasses = uniq(allLines.map((l) => l.medical_aid_class));
  const admitted = allLines.map((l) => l.admitted_on).find(Boolean);
  const sexLabel = (x?: string | null) => (x === "M" ? "남" : x === "F" ? "여" : x ?? "");
  return (
    <Card title={`진료비 명세 — ${d.chart_no} (${d.care_month})`}
      action={<button onClick={onClose} className="text-xs text-slate-400 hover:text-slate-600">닫기</button>}>
      <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1 rounded-md bg-slate-50 px-3 py-2 text-sm">
        <span>청구 <b>{won(s.total)}</b></span>
        <span className="text-blue-700">본인부담 <b>{won(s.patient_share)}</b></span>
        <span className="text-slate-500">보험자 {won(s.insurer_share)}</span>
        {s.non_covered > 0 && <span className="text-rose-600">비급여 {won(s.non_covered)}</span>}
        <span className="ml-auto">기수납 {won(s.prepaid)} → <b>수납예정 {won(s.balance_due)}</b></span>
      </div>
      {d.institution_grade && (
        <p className="mb-2 text-[11px] text-slate-400">
          단가는 {d.institution_grade} 종별가산 반영분 · 본인부담률 {d.encounter_type === "INPATIENT" ? "입원 20%" : `외래(${d.institution_grade})`}
        </p>
      )}
      <p className="mb-2 text-xs">
        <span className="font-medium text-slate-600">상병(진단):</span>{" "}
        {(d.diagnosis_codes ?? []).length > 0
          ? (d.diagnosis_codes ?? []).map((c) => (
              <code key={c} className="mr-1 rounded bg-slate-100 px-1 text-[11px] text-slate-600">{c}</code>))
          : <span className="text-slate-400">HIS 미수신 — 의학적 타당성은 의료진 질의로 확인</span>}
      </p>
      {(demo || specials.length > 0 || aidClasses.length > 0 || admitted) && (
        <p className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
          {demo && (
            <span><span className="font-medium text-slate-600">환자:</span>{" "}
              {demo.age != null ? `${demo.age}세` : ""}
              {demo.sex ? `${demo.age != null ? " · " : ""}${sexLabel(demo.sex)}` : ""}</span>
          )}
          {admitted && <span className="text-slate-500">입원일 {String(admitted).slice(0, 10)}</span>}
          {specials.map((sp) => <Badge key={sp} tone="blue">산정특례 {sp}</Badge>)}
          {aidClasses.map((a) => <Badge key={a} tone="amber">의료급여 {a}종</Badge>)}
        </p>
      )}
      {d.warnings.length > 0 && (
        <ul className="mb-2 space-y-0.5">{d.warnings.map((w, i) =>
          <li key={i} className="text-xs text-amber-600">⚠ {w}</li>)}</ul>
      )}
      {d.groups.map((g) => (
        <div key={g.category} className="mb-3">
          <div className="mb-1 flex justify-between border-b border-slate-200 pb-0.5 text-xs font-semibold text-slate-600">
            <span>{CLIN_ICON[g.category] ?? "▫️"} {g.label}</span>
            <span>소계 청구 {won(g.claimed)} · 본인 {won(g.patient)}</span>
          </div>
          {g.lines.map((ln, i) => (
            <div key={i} className="border-b border-slate-50 py-1.5 text-sm">
              <div className="flex flex-wrap items-baseline gap-x-2">
                {ln.treated_on && (
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-500"
                    title={ln.treated_at ? `진료시각 ${String(ln.treated_at).slice(0, 16).replace("T", " ")}` : "진료일"}>
                    {String(ln.treated_on).slice(0, 10)}</span>
                )}
                <code className="text-xs font-medium">{ln.code}</code>
                {ln.name && <span className="text-slate-500">{ln.name}</span>}
                {/* 투약 라인 — 약품명·성분 노출(병용금기·중복·성분중복 심사) */}
                {ln.drug_name && (
                  <span className="rounded bg-emerald-50 px-1 text-[11px] text-emerald-700"
                    title={ln.drug_ingredient ? `성분: ${ln.drug_ingredient}` : undefined}>
                    💊 {ln.drug_name}
                    {ln.drug_ingredient && (
                      <span className="ml-1 text-emerald-500">({ln.drug_ingredient})</span>)}
                  </span>
                )}
                {ln.time_surcharge && <Badge tone="amber">{ln.time_surcharge}</Badge>}
                <span className="text-xs text-slate-400">×{ln.count}</span>
                <span className="ml-auto font-medium">{won(ln.total)}</span>
                {ln.findings.map((f) => (
                  <Badge key={f.id} tone={f.resolved ? "gray" : f.severity === "BLOCK" ? "red" : "amber"}>
                    {f.resolved ? "해소" : f.severity}</Badge>
                ))}
              </div>
              <div className="mt-0.5 font-mono text-xs text-slate-500">
                {ln.formula}
                {!ln.priced && <span className="text-amber-600"> · 단가 미해소(0원 처리)</span>}
                {ln.priced && <span className="text-slate-400"> · {SRC[ln.price_source] ?? ln.price_source}</span>}
                {ln.fee_effective_from && (
                  <span className="text-slate-400"> · 단가근거 {ln.fee_effective_from} 시행
                    {ln.fee_valid_to ? `~${ln.fee_valid_to}` : "(현행)"}</span>
                )}
              </div>
              <div className="flex items-center text-xs text-slate-500">
                본인 {won(ln.patient)} / 보험 {won(ln.insurer)}
                <span className="text-slate-400"> · {ln.rate_basis}</span>
                <button onClick={() => aiDiag.mutate({ code: ln.code,
                  context: `${ln.code} ${ln.name ?? ""} ${ln.count}회` })}
                  disabled={aiDiag.isPending}
                  className="ml-auto rounded bg-violet-50 px-1.5 py-0.5 text-[11px] text-violet-600 hover:bg-violet-100 disabled:opacity-50">
                  {aiDiag.isPending && aiDiag.variables?.code === ln.code ? "🔍 조회 중…" : "🔍 근거 검색"}</button>
                <button onClick={() => openComposer({ kind: "ask", id: 0, line_code: ln.code,
                  finding_id: ln.findings.find((f) => !f.resolved)?.id })}
                  className="rounded bg-indigo-50 px-1.5 py-0.5 text-[11px] text-indigo-600 hover:bg-indigo-100">
                  💬 의료진 질의</button>
              </div>
              {aiDiag.isPending && aiDiag.variables?.code === ln.code && (
                <div className="mt-0.5 animate-pulse rounded bg-violet-50 px-2 py-0.5 text-[11px] text-violet-500">
                  🔍 공시·산정기준 근거를 조회 중…</div>
              )}
              {ln.findings.filter((f) => !f.resolved).map((f) => (
                <div key={f.id} className="mt-0.5 rounded bg-rose-50 px-2 py-0.5 text-xs text-rose-700">
                  ⚠ {ln.treated_on && <span className="font-mono">[{String(ln.treated_on).slice(5, 10)}] </span>}
                  {f.rule_name}: {f.detail} {f.amount > 0 && `· 위험액 ${won(f.amount)}`}
                </div>
              ))}
              {composer?.kind === "ask" && composer.line_code === ln.code &&
                renderComposer(`의료진 질의 (${ln.code}) — 근거/수정 요청 내용`, "indigo", true)}
            </div>
          ))}
        </div>
      ))}
      {d.groups.length === 0 && <p className="py-3 text-sm text-slate-400">해당 진료월 라인 없음</p>}

      {/* 급여분류 소계(청구 관점) — 임상영역 그룹과 별개로 행위료/약제비/재료대 등 청구 소계 요약 */}
      {(d.benefit_summary ?? []).length > 0 && (
        <div className="mt-2 rounded-md bg-slate-50 px-3 py-2">
          <div className="mb-1 text-[11px] font-semibold text-slate-500">급여분류 소계 (청구 관점)</div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-slate-600">
            {(d.benefit_summary ?? []).map((b) => (
              <span key={b.category}>{b.label} <b>{won(b.claimed)}</b></span>
            ))}
          </div>
        </div>
      )}

      {/* 공시 근거 — 라인별 평가 결과 */}
      {diag && (
        <div className="mt-3 rounded-md border border-violet-200 bg-violet-50/50 p-2">
          <div className="flex items-center gap-2 text-sm">
            <span className="font-semibold">근거 검색 — {diag.code}</span>
            {diag.available === false ? <span className="text-slate-500">{diag.note}</span> : (
              <Badge tone={diag.risk_level === "HIGH" ? "red" : diag.risk_level === "MEDIUM" ? "amber" : "gray"}>
                삭감위험 {diag.risk_level ?? "?"}{diag.risk_score != null ? ` ${diag.risk_score}` : ""}</Badge>
            )}
            {diag.attempted != null && diag.attempted > 1 && (
              <span className={`text-[10px] ${diag.degraded ? "text-amber-600" : "text-slate-400"}`}
                title="비결정성 완화 — N회 호출 다수결(성공/시도)">
                다수결 {diag.samples ?? 0}/{diag.attempted}회
                {diag.risk_votes ? ` (${Object.entries(diag.risk_votes).map(([k, v]) => `${k}${v}`).join("·")})` : ""}
                {diag.degraded ? " ⚠ 표본부족" : ""}</span>)}
            {diag.held && <span className="text-xs text-slate-500">판단보류(근거부족)</span>}
            {diag.held && aiDiag.variables && (
              <button onClick={() => aiDiag.mutate(aiDiag.variables!)} disabled={aiDiag.isPending}
                className="rounded bg-violet-500 px-2 py-0.5 text-xs text-white disabled:opacity-40">
                {aiDiag.isPending ? "재조회 중…" : "다시 조회"}</button>)}
            <button onClick={() => setDiag(null)} className="ml-auto text-xs text-slate-400">닫기</button>
          </div>
          {diag.recommendation && (
            <p className="mt-1 text-xs font-medium text-slate-700">권고: {diag.recommendation}</p>)}
          {/* 상세 근거(weve reasoning) — 왜 그 위험도/권고인지 */}
          {diag.reasoning && (
            <p className="mt-1 whitespace-pre-line text-xs text-slate-600">{diag.reasoning}</p>)}
          {(diag.evidence_citations ?? []).map((e, i) => (
            <div key={i} className="mt-1 border-l-2 border-violet-300 pl-2 text-xs">
              <span className="font-medium">{e.title}</span>
              {e.notice_type && <span className="ml-1 rounded bg-violet-100 px-1 text-[10px] text-violet-700">{e.notice_type}</span>}
              {e.effective_from && <span className="text-slate-400"> · {e.effective_from} 시행</span>}
              {e.score != null && <span className="text-slate-400"> · 유사도 {(e.score * 100).toFixed(0)}%</span>}
              {e.relevant_passage && <p className="text-slate-500">{e.relevant_passage.slice(0, 200)}</p>}
            </div>
          ))}
          {diag.held && (
            <p className="mt-1 text-[11px] text-slate-400">
              근거 고시 미확인 — 로컬 룰만 적용. 근거검색은 비결정적이라 ‘다시 조회’로 재시도하세요.</p>)}
          <p className="mt-1 text-[10px] text-slate-400">
            참고 정보(근거검색·휴먼인더루프) — 최종 청구 판단은 심사자 검토에 따릅니다.</p>
        </div>
      )}

      {/* 자유 공시 질의 (weve 근거 검색) */}
      <div className="mt-3 rounded-md border border-slate-100 p-2">
        <p className="mb-1 text-xs font-semibold text-slate-600">공시·산정기준 질의 (보조)</p>
        <div className="flex gap-1">
          <input value={nq} onChange={(e) => setNq(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && setNquery(nq)}
            placeholder="예: 재진진찰료 산정기준, 입원료 종별가산"
            className="flex-1 rounded-md border border-slate-300 px-2 py-1 text-sm" />
          <button onClick={() => setNquery(nq)}
            className="rounded-md bg-violet-600 px-3 py-1 text-xs font-medium text-white">질의</button>
        </div>
        {notices.isFetching && <p className="mt-1 text-xs text-slate-400">검색 중…</p>}
        {(notices.data?.items ?? []).map((n, i) => (
          <div key={i} className="mt-1 border-l-2 border-slate-200 pl-2 text-xs">
            <span className="font-medium">{n.title}</span>
            {n.effective_from && <span className="text-slate-400"> · {n.effective_from} 시행</span>}
            <span className="text-slate-400"> · {n.notice_type}</span>
            {n.snippet && <p className="text-slate-500">{n.snippet}</p>}
          </div>
        ))}
        {notices.data && notices.data.items.length === 0 && (
          <p className="mt-1 text-xs text-slate-400">관련 공시 없음 ({notices.data.source})</p>
        )}
      </div>

      {/* 협업 타임라인 (P2 — 삭감 방지) */}
      <div className="mt-4 border-t border-slate-200 pt-3">
        <p className="mb-2 text-sm font-semibold text-slate-700">
          의료진 협업 ({d.queries.length}건) — 근거 합의로 삭감 사전 제거</p>
        {d.queries.length === 0 && (
          <p className="text-xs text-slate-400">라인의 &quot;💬 의료진 질의&quot;로 근거·수정을 요청하세요.</p>
        )}
        {d.queries.map((q) => (
          <div key={q.id} className="mb-2 rounded-md border border-slate-100 p-2">
            <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
              {q.line_code && <code className="text-xs">{q.line_code}</code>}
              <Badge tone={QSTATUS[q.status]?.tone ?? "gray"}>{QSTATUS[q.status]?.label ?? q.status}</Badge>
              <span>{q.question}</span>
              <span className="text-[11px] text-slate-400">#{q.raised_by}
                {q.assigned_to ? ` → 의료진#${q.assigned_to}` : ""}</span>
            </div>
            {q.replies.map((r) => (
              <div key={r.id} className="ml-3 mt-1 border-l-2 border-slate-200 pl-2 text-xs">
                <Badge tone={r.reply_type === "EVIDENCE" ? "blue" : "amber"}>
                  {r.reply_type === "EVIDENCE" ? "근거" : "수정"}</Badge>
                <span className="ml-1 text-slate-600">{r.body}</span>
                <span className="ml-1 text-slate-400">· 의료진#{r.author}</span>
              </div>
            ))}
            {q.resolution_note && (
              <p className="ml-3 mt-1 text-xs text-green-700">종결: {q.resolution_note}</p>
            )}
            {q.status !== "ACCEPTED" && q.status !== "REVISED" && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                <Button size="sm" onClick={() => openComposer({ kind: "evidence", id: q.id })}>
                  근거 회신</Button>
                <Button variant="warning" size="sm"
                  onClick={() => openComposer({ kind: "revise", id: q.id })}>수정 통보</Button>
                <Button variant="success" size="sm"
                  onClick={() => openComposer({ kind: "resolve", id: q.id })}>근거 인정→해소</Button>
                <Button variant="neutral" size="sm"
                  onClick={() => resolve.mutate({ id: q.id, status: "REVISED", note: "오더 수정 완료" })}>
                  수정 완료</Button>
              </div>
            )}
            {composer?.id === q.id && composer.kind === "evidence" &&
              renderComposer("의료진 회신 — 근거 제시", "blue")}
            {composer?.id === q.id && composer.kind === "revise" &&
              renderComposer("의료진 회신 — 오더 수정 통보", "amber")}
            {composer?.id === q.id && composer.kind === "resolve" &&
              renderComposer("근거 인정 — 적발 해소 사유(의료진 근거)", "green", true)}
          </div>
        ))}
      </div>
    </Card>
  );
}


// ── 의료진 질의 관리 (협업 로그) — 전역 질의 기록·상태·내용 추적 ──
interface QueryRow { id: number; case_id: number; chart_no: string | null;
  care_month: string | null; line_code: string | null; status: string; question: string;
  raised_by: number; assigned_to: number | null; resolution_note: string | null; created_at: string;
  replies: { id: number; reply_type: string; body: string; author: number; created_at: string }[] }

const Q_FILTERS: [string, string][] = [["", "전체"], ["OPEN", "질의중"],
  ["ANSWERED", "회신됨"], ["ACCEPTED", "근거인정"], ["REVISED", "수정완료"]];

export function QueriesTab({ onOpenCase }: { onOpenCase: (caseId: number) => void }) {
  const [status, setStatus] = useState("");
  const q = useQuery({ queryKey: ["pr-queries", status], queryFn: () =>
    api<{ count: number; items: QueryRow[] }>(
      `/claims/pre-review/queries${status ? `?status=${status}` : ""}`) });
  const ts = (s: string) => String(s).slice(0, 16).replace("T", " ");

  return (
    <Card title={`의료진 질의 관리 (${q.data?.count ?? 0}건) — 협업 로그`}
      action={<span className="text-[11px] text-slate-400">생성·회신·종결은 케이스 명세에서 · 전부 감사기록</span>}>
      <div className="mb-3 flex gap-1">
        {Q_FILTERS.map(([k, label]) => (
          <button key={k} onClick={() => setStatus(k)}
            className={`rounded-md px-2.5 py-1 text-xs font-medium ${status === k
              ? "bg-indigo-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
            {label}</button>
        ))}
      </div>
      {q.isLoading && <p className="py-3 text-sm text-slate-400">불러오는 중…</p>}
      {q.data && q.data.items.length === 0 && (
        <p className="py-3 text-sm text-slate-400">질의 없음 — 명세의 &quot;💬 의료진 질의&quot;로 생성됩니다.</p>)}
      <div className="space-y-2">
        {(q.data?.items ?? []).map((qr) => (
          <div key={qr.id} className="rounded-md border border-slate-100 p-2.5">
            <div className="flex flex-wrap items-baseline gap-x-2 text-sm">
              <Badge tone={QSTATUS[qr.status]?.tone ?? "gray"}>{QSTATUS[qr.status]?.label ?? qr.status}</Badge>
              {qr.chart_no && <span className="font-medium">{qr.chart_no}</span>}
              {qr.care_month && <span className="text-xs text-slate-400">{qr.care_month}</span>}
              {qr.line_code && <code className="text-xs text-slate-500">{qr.line_code}</code>}
              <span className="text-[11px] text-slate-400">
                #{qr.raised_by}{qr.assigned_to ? ` → 의료진#${qr.assigned_to}` : ""}</span>
              <span className="ml-auto text-[10px] text-slate-400">{ts(qr.created_at)}</span>
            </div>
            <p className="mt-1 text-sm text-slate-700">{qr.question}</p>
            {qr.replies.map((r) => (
              <div key={r.id} className="ml-3 mt-1 border-l-2 border-slate-200 pl-2 text-xs">
                <Badge tone={r.reply_type === "EVIDENCE" ? "blue" : "amber"}>
                  {r.reply_type === "EVIDENCE" ? "근거" : "수정"}</Badge>
                <span className="ml-1 text-slate-600">{r.body}</span>
                <span className="ml-1 text-slate-400">· 의료진#{r.author} · {ts(r.created_at)}</span>
              </div>
            ))}
            {qr.resolution_note && (
              <p className="ml-3 mt-1 text-xs text-green-700">종결: {qr.resolution_note}</p>)}
            <div className="mt-1.5">
              <button onClick={() => onOpenCase(qr.case_id)}
                className="rounded bg-slate-600 px-2 py-0.5 text-xs text-white hover:bg-slate-700">
                케이스 명세 열기 →</button>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}


// ── 자동 처리 근거·로그 (자동 심사 P1/P2 검토·오버라이드) ──
interface AutoCleared { case_id: number; chart_no: string; care_month: string;
  status: string; claimed_amt: number; last_run_at: string | null }
interface AutoDismissed { finding_id: number; case_id: number; chart_no: string;
  care_month: string; rule_name: string; code: string | null; amount: number;
  note: string | null; decided_at: string | null; resolved: boolean }

export function AutoActionsTab({ onOpenCase }: { onOpenCase: (caseId: number) => void }) {
  const q = useQuery({ queryKey: ["pr-auto"], queryFn: () =>
    api<{ auto_cleared: AutoCleared[]; auto_dismissed: AutoDismissed[] }>(
      "/claims/pre-review/auto-actions") });
  const ts = (s: string | null) => (s ? String(s).slice(0, 16).replace("T", " ") : "-");
  const ac = q.data?.auto_cleared ?? [];
  const ad = q.data?.auto_dismissed ?? [];

  return (
    <div className="space-y-4">
      <p className="text-[11px] text-slate-400">
        자동 심사가 처리한 건의 <b>근거·시각</b>입니다. 되돌리려면 &quot;케이스 열기&quot;에서
        상태/결정을 변경하세요. 전체 불변 이력은 감사로그(AUTO_CLEAR·AUTO_DISMISS).
      </p>
      <Card title={`🤖 자동 이상없음 (${ac.length}건) — 근거: 무적발·리스크0`}>
        {q.isLoading ? <p className="py-2 text-sm text-slate-400">불러오는 중…</p>
          : ac.length === 0 ? <p className="py-2 text-sm text-slate-400">없음</p>
          : <Table headers={["차트", "진료월", "상태", "청구액", "처리시각", ""]}
              rows={ac.map((c) => [c.chart_no, c.care_month,
                <Badge key="s" tone={c.status === "CLEARED" ? "green" : "amber"}>{c.status}</Badge>,
                won(c.claimed_amt), ts(c.last_run_at),
                <button key="o" onClick={() => onOpenCase(c.case_id)}
                  className="rounded bg-slate-600 px-2 py-0.5 text-xs text-white hover:bg-slate-700">열기</button>])} />}
      </Card>
      <Card title={`🤖 자동 오탐 (${ad.length}건) — 근거: 확정 오탐 패턴(캘리브레이션)`}
        action={<span className="text-[11px] text-slate-400">P2 옵트인 — 미활성 시 0건</span>}>
        {ad.length === 0 ? <p className="py-2 text-sm text-slate-400">없음 (P2 미활성이거나 패턴 미충족)</p>
          : <Table headers={["차트", "룰", "코드", "위험액", "근거", "처리시각", ""]}
              rows={ad.map((f) => [f.chart_no, f.rule_name, f.code ?? "-", won(f.amount),
                <span key="n" className="text-xs text-slate-500" title={f.note ?? ""}>
                  {(f.note ?? "").slice(0, 40)}{(f.note ?? "").length > 40 ? "…" : ""}</span>,
                ts(f.decided_at),
                <button key="o" onClick={() => onOpenCase(f.case_id)}
                  className="rounded bg-slate-600 px-2 py-0.5 text-xs text-white hover:bg-slate-700">열기</button>])} />}
      </Card>
    </div>
  );
}
