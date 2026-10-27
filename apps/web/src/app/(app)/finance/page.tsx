"use client";
/** 재무 — 전표(승인 워크플로 M2)·예산 집행률(FR-09)·월별 정산. */
import { todayLocal, monthLocal } from "@/lib/dates";
import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useUrlState } from "@/lib/use-url-state";
import { useNameMap } from "@/lib/use-name-map";
import { api } from "@/lib/api";
import { Badge, Banner, Button, Card, Input, MessageBanner, Table } from "@/components/ui";
import { useInputDialog } from "@/components/input-dialog";
import { useConfirm } from "@/components/confirm";
import { AttachmentCell } from "@/components/attachment-cell";
import { AccountPicker } from "@/components/account-picker";
import { won } from "@/lib/format";

interface Voucher { id: number; voucher_on: string; source: string; source_ref: string;
                    status: string; memo: string | null; amount: number;
                    // 통제 증적 — 모델엔 있었으나 응답에서 탈락해 SoD 준수·역분개 연결·반려 사유를
                    // 화면에서 확인할 수 없었다.
                    created_by: number | null; submitted_by: number | null;
                    approved_by: number | null; approved_at: string | null;
                    reject_reason: string | null;
                    reverses: number | null; reversed_by: number | null }
interface VLine { account_code: string; side: "DEBIT" | "CREDIT"; amount: number;
  detail: string | null; cost_center: string | null; project: string | null;
  department_id: number | null }
type Line = { id: number; account_code: string; side: "DEBIT" | "CREDIT"; amount: string;
  cost_center?: string; project?: string };

// 행 추가/중간삭제 시 React 재조정이 안정적이도록 행마다 불변 id 부여(인덱스 key 금지).
let lineSeq = 0;
const blankLine = (side: Line["side"]): Line =>
  ({ id: lineSeq++, account_code: "", side, amount: "" });

const STATUS_TONE: Record<string, "gray" | "green" | "red" | "amber" | "blue"> = {
  DRAFT: "gray", PENDING: "amber", POSTED: "green", REJECTED: "red", REVERSED: "red",
};

/** 전표 출처(source_ref) → 원천 화면 링크(X2-2). 인식 못하면 null(일반 텍스트). */
function sourceHref(source: string, ref: string): string | null {
  if (source === "patient") {
    const m = ref.match(/^closing-(\d{4}-\d{2}-\d{2})-/);   // 일마감 전표 → 마감 화면
    if (m) return `/patients/closing?date=${m[1]}`;
  }
  return null;
}

export default function FinancePage() {
  const yyyymm = monthLocal();
  const [year] = useState(new Date().getFullYear());
  const [banner, setBanner] = useState<Banner>(null);
  // 전표 라인 펼침 — URL(?voucher=)로 백업: 마감 등에서 ?voucher=42로 진입 시 자동 펼침·공유(X2-2).
  const [voucherParam, setVoucherParam] = useUrlState("voucher", "");
  const expandedId = voucherParam ? Number(voucherParam) : null;
  const setExpandedId = (id: number | null) => setVoucherParam(id ? String(id) : "");
  const deptName = useNameMap("departments", "/departments");   // 예산 부서 ID→이름(X2-6)
  const qc = useQueryClient();
  const { ask, dialog } = useInputDialog();
  const confirm = useConfirm();

  // 수동 전표 초안 작성 (M2 — POST /vouchers → DRAFT)
  const [memo, setMemo] = useState("");
  const [on, setOn] = useState(todayLocal());
  const [lines, setLines] = useState<Line[]>(() => [blankLine("DEBIT"), blankLine("CREDIT")]);
  const setLine = (i: number, patch: Partial<Line>) =>
    setLines((ls) => ls.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const sum = (side: "DEBIT" | "CREDIT") =>
    lines.filter((l) => l.side === side).reduce((s, l) => s + (Number(l.amount) || 0), 0);
  const balanced = sum("DEBIT") > 0 && sum("DEBIT") === sum("CREDIT");

  const vouchers = useQuery({
    queryKey: ["vouchers"],
    queryFn: () => api<{ items: Voucher[] }>("/finance/vouchers?size=50"),
  });
  const [dimType, setDimType] = useState<"cost_center" | "project">("cost_center");
  const dimSummary = useQuery({
    queryKey: ["dim-summary", dimType, year],
    queryFn: () => api<{ items: { value: string; debit: number; credit: number; net: number }[] }>(
      `/finance/dimensions/summary?dim=${dimType}&since=${year}-01-01&until=${year}-12-31`),
  });
  const execution = useQuery({
    queryKey: ["budget-exec", year],
    queryFn: () => api<{ items: { department_id: number; account_code: string;
      budget: number; spent: number; committed: number; available: number;
      used_rate: number; status: string }[] }>(
      `/finance/budgets/execution?fiscal_year=${year}`),
  });
  const monthly = useQuery({
    queryKey: ["monthly", yyyymm],
    queryFn: () => api<{ accounts: Record<string, { debit: number; credit: number }> }>(
      `/finance/reports/monthly/${yyyymm}`),
  });

  // 전표 승인=GL 전기 → 파생 재무 카드도 무효화(월별정산·예산통제·차원집계). 종전 vouchers만
  // 무효화해 승인 후 이 카드들이 전기 전 낡은 수치를 표시하던 갭(적대검토 MED).
  const refresh = () => {
    for (const k of [["vouchers"], ["monthly"], ["budget-exec"], ["dim-summary"]])
      qc.invalidateQueries({ queryKey: k });
  };

  const createDraft = useMutation({
    mutationFn: () => api<{ id: number; status: string }>("/finance/vouchers", {
      method: "POST",
      body: JSON.stringify({ voucher_on: on, memo,
        lines: lines.map((l) => ({ account_code: l.account_code, side: l.side,
          amount: Number(l.amount),
          cost_center: l.cost_center?.trim() || null, project: l.project?.trim() || null })) }),
    }),
    onSuccess: (d) => {
      setBanner({ text: `초안 생성 #${d.id} (${d.status}) — 상신 후 승인 필요`, tone: "ok" });
      setMemo(""); setLines([blankLine("DEBIT"), blankLine("CREDIT")]);
      refresh();
    },
    onError: (e: { message?: string }) =>
      setBanner({ text: e?.message ?? "초안 생성 실패", tone: "error" }),
  });

  const act = useMutation({
    mutationFn: ({ id, action, reason }: { id: number; action: string; reason?: string }) =>
      api<{ status: string }>(`/finance/vouchers/${id}/${action}`, {
        method: "POST", body: reason !== undefined ? JSON.stringify({ reason }) : undefined }),
    onSuccess: (d, v) => {
      const label = { submit: "상신", approve: "승인", reject: "반려" }[v.action] ?? v.action;
      setBanner({ text: `전표 #${v.id} ${label} 완료 (${d.status})`, tone: "ok" });
      refresh();
    },
    onError: (e: { message?: string }) =>
      setBanner({ text: e?.message ?? "처리 실패", tone: "error" }),
  });

  const onApprove = async (v: Voucher) => {
    if (await confirm({ title: "전표 승인", body: `전표 #${v.id}을(를) 승인합니다. GL 원장에 전기됩니다.`,
      confirmLabel: "승인" })) act.mutate({ id: v.id, action: "approve" });
  };
  const rowActions = (v: Voucher) => {
    // 처리 중인 행만 로딩(단일 isPending이 전 행을 비활성화하던 것·P0-4)
    const busy = act.isPending && act.variables?.id === v.id;
    if (v.status === "DRAFT")
      return <Button variant="secondary" isLoading={busy}
        onClick={() => act.mutate({ id: v.id, action: "submit" })}>상신</Button>;
    if (v.status === "PENDING")
      return (
        <span className="flex gap-1">
          <Button isLoading={busy} onClick={() => onApprove(v)}>승인</Button>
          <Button variant="danger" isLoading={busy} onClick={async () => {
            const reason = await ask({ title: "전표 반려", label: "반려 사유",
              kind: "text", required: true, confirmLabel: "반려" });
            if (typeof reason === "string" && reason) act.mutate({ id: v.id, action: "reject", reason });
          }}>반려</Button>
        </span>);
    return <span className="text-slate-300">—</span>;
  };

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">재무 — 전표·예산·정산</h1>
      <MessageBanner banner={banner} />

      {(vouchers.isError || execution.isError || monthly.isError) && (
        <p className="text-sm text-red-600">재무 데이터를 불러오지 못했습니다 (권한 확인)</p>)}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`예산 통제 — 예실대비·약정 (${year})`} action={
          <span className="text-[11px] text-slate-400">가용 = 예산 − 집행 − 약정</span>}>
          <Table headers={["부서", "계정", "예산", "집행", "약정", "가용", "상태"]}
            rows={(execution.data?.items ?? []).map((b) => [
              deptName(b.department_id), b.account_code,
              b.budget.toLocaleString(), b.spent.toLocaleString(),
              (b.committed ?? 0).toLocaleString(),
              <span key="a" className={b.available < 0 ? "font-bold text-red-600" : ""}>
                {b.available.toLocaleString()}</span>,
              <Badge key="r" tone={b.status === "OVER" ? "red" : b.status === "WARNING" ? "amber" : "green"}>
                {b.status === "OVER" ? "초과" : b.status === "WARNING" ? `주의 ${b.used_rate}%` : `정상 ${b.used_rate}%`}</Badge>,
            ])} />
        </Card>

        <Card title={`월별 정산 (${yyyymm} — 역분개 제외)`}>
          <Table headers={["계정", "차변", "대변"]}
            rows={Object.entries(monthly.data?.accounts ?? {}).map(([code, v]) => [
              code, v.debit.toLocaleString(), v.credit.toLocaleString()])} />
        </Card>
      </div>

      <Card title="수동 전표 작성 (초안 → 상신 → 승인)" action={
        <span className="text-[11px] text-slate-400">작성자 ≠ 승인자(직무분리)</span>}>
        <div className="space-y-2">
          <div className="grid gap-2 sm:grid-cols-2">
            <Input label="전표일자" type="date" value={on} onChange={(e) => setOn(e.target.value)} />
            <Input label="적요" value={memo} onChange={(e) => setMemo(e.target.value)}
              placeholder="예: 비품 구입" />
          </div>
          {lines.map((l, i) => (
            <div key={l.id} className="grid grid-cols-[1fr_auto_1fr_110px_110px_auto] items-end gap-2">
              <label className="block">
                {i === 0 && <span className="mb-1 block text-xs font-medium text-slate-600">계정 (코드·명 검색)</span>}
                <AccountPicker value={l.account_code}
                  onPick={(code) => setLine(i, { account_code: code })} />
              </label>
              <label className="block">
                {i === 0 && <span className="mb-1 block text-xs font-medium text-slate-600">차/대</span>}
                <select value={l.side}
                  onChange={(e) => setLine(i, { side: e.target.value as Line["side"] })}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                  <option value="DEBIT">차변</option><option value="CREDIT">대변</option>
                </select>
              </label>
              <Input label={i === 0 ? "금액" : undefined} type="number" value={l.amount}
                onChange={(e) => setLine(i, { amount: e.target.value })} placeholder="0" />
              <Input label={i === 0 ? "코스트센터" : undefined} value={l.cost_center ?? ""}
                onChange={(e) => setLine(i, { cost_center: e.target.value })} placeholder="(선택)" />
              <Input label={i === 0 ? "프로젝트" : undefined} value={l.project ?? ""}
                onChange={(e) => setLine(i, { project: e.target.value })} placeholder="(선택)" />
              <Button variant="secondary" type="button"
                onClick={() => setLines((ls) => (ls.length > 2 ? ls.filter((_, j) => j !== i) : ls))}>−</Button>
            </div>
          ))}
          <div className="flex items-center justify-between text-xs">
            <Button variant="secondary" type="button"
              onClick={() => setLines((ls) => [...ls, blankLine("DEBIT")])}>
              + 행 추가</Button>
            <span className={balanced ? "text-emerald-600" : "text-slate-500"}>
              차변 {sum("DEBIT").toLocaleString()} / 대변 {sum("CREDIT").toLocaleString()}
              {balanced ? " ✓ 균형" : " — 불일치"}</span>
          </div>
          <Button isLoading={createDraft.isPending} disabled={!balanced}
            onClick={() => createDraft.mutate()}>초안 생성</Button>
        </div>
      </Card>

      <Card title="전표 (출처 추적 — patient/claims/scm/manual)">
        <Table headers={["#", "일자", "출처", "참조", "금액", "상태", "적요", "증빙", "분개", "조치"]}
          rows={(vouchers.data?.items ?? []).map((v) => [
            v.id, v.voucher_on,
            <Badge key="s" tone="blue">{v.source}</Badge>,
            sourceHref(v.source, v.source_ref)
              ? <Link key="r" href={sourceHref(v.source, v.source_ref)!}
                  className="font-mono text-xs text-blue-600 hover:underline">{v.source_ref} ↗</Link>
              : <span key="r" className="font-mono text-xs">{v.source_ref}</span>,
            won(v.amount),
            <span key="st" className="flex flex-wrap items-center gap-1">
              <Badge tone={STATUS_TONE[v.status] ?? "gray"}>{v.status}</Badge>
              {/* REVERSED 배지만 있고 어느 전표가 뒤집었는지 링크가 없어 정정 추적이 끊겼다 */}
              {v.reversed_by && <span className="text-xs text-slate-500">→ #{v.reversed_by}</span>}
              {v.reverses && <span className="text-xs text-slate-500">역분개 of #{v.reverses}</span>}
            </span>,
            // 반려 사유는 필수 입력인데 REJECTED 행에서 다시 볼 방법이 없었다
            v.reject_reason
              ? <span key="m" className="text-xs text-rose-600" title={v.reject_reason}>
                  {v.memo ?? "-"} · 반려: {v.reject_reason}</span>
              : (v.memo ?? "-"),
            <AttachmentCell key="att" entityType="voucher" entityId={v.id} />,
            <Button key="ln" variant="secondary" type="button"
              onClick={() => setExpandedId(expandedId === v.id ? null : v.id)}>
              {expandedId === v.id ? "닫기" : "보기"}</Button>,
            rowActions(v),
          ])} />
        {expandedId !== null && <VoucherLines voucherId={expandedId} />}
      </Card>

      <Card title={`차원별 집계 (${year} · G9)`} action={
        <select value={dimType} onChange={(e) => setDimType(e.target.value as "cost_center" | "project")}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm">
          <option value="cost_center">코스트센터</option>
          <option value="project">프로젝트</option>
        </select>}>
        <Table headers={[dimType === "cost_center" ? "코스트센터" : "프로젝트", "차변", "대변", "순액"]}
          rows={(dimSummary.data?.items ?? []).map((d) => [
            d.value, d.debit.toLocaleString(), d.credit.toLocaleString(), d.net.toLocaleString()])}
          empty={dimSummary.isFetching ? "불러오는 중…" : "차원이 지정된 전표가 없습니다(전표 라인에 코스트센터·프로젝트 입력)"} />
      </Card>
    </div>
  );
}

/** 전표 분개 라인 — 승인 전 계정·금액 확인(내부통제). */
function VoucherLines({ voucherId }: { voucherId: number }) {
  const q = useQuery({
    queryKey: ["voucher-lines", voucherId],
    queryFn: () => api<{ draft: boolean; items: VLine[] }>(`/finance/vouchers/${voucherId}/lines`),
  });
  if (q.isPending) return <p className="mt-2 text-xs text-slate-400">분개 불러오는 중…</p>;
  if (q.isError) return <p className="mt-2 text-xs text-red-600">분개를 불러오지 못했습니다</p>;
  return (
    <div className="mt-3 rounded-md border border-slate-200 bg-slate-50 p-3">
      <p className="mb-2 flex items-center gap-2 text-xs font-semibold text-slate-600">
        전표 #{voucherId} 분개
        {/* 승인 전 전표는 원장 라인이 없고 초안(JSON)만 있다 — 미전기임을 명시 */}
        {q.data?.draft && <Badge tone="amber">초안(미전기)</Badge>}</p>
      <Table headers={["계정", "차/대", "금액", "적요", "코스트센터", "프로젝트"]}
        rows={(q.data?.items ?? []).map((l) => [
          l.account_code,
          l.side === "DEBIT" ? "차변" : "대변",
          won(l.amount),
          l.detail ?? "-", l.cost_center ?? "-", l.project ?? "-",
        ])} empty="분개 라인이 없습니다" />
    </div>
  );
}
