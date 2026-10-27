"use client";
/** 통합 결재함(X2-4) — N단계 결재선의 "내 결재 대기"를 단일 화면으로. 승인이 전표·발주·위수탁·
 * ITAM 폐기 등 여러 화면에 산재하던 것을 한 곳에 모으고, 각 건은 실제 결재 화면으로 바로가기.
 * (실제 approve/reject는 도메인 객체 전이 동반이라 도메인 화면에서 수행) */
import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Modal, Table } from "@/components/ui";
import { QueryError, QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

interface Item {
  instance_id: number; doc_type: string; doc_id: number;
  amount: number | null; step_no: number; approver_type: string;
}
const DOC: Record<string, { ko: string; href: string }> = {
  PO: { ko: "구매 발주", href: "/inventory" },
  OUTSOURCING_CONTRACT: { ko: "위수탁 계약", href: "/inventory-contracts" },
  MEDICAL_CONTRACT: { ko: "의료진 계약", href: "/hr/medical-contracts" },
  ITAM_DISPOSAL: { ko: "IT 자산 폐기", href: "/assets/it" },
  VOUCHER: { ko: "회계 전표", href: "/finance" },
  // v1.203 — 결재엔진 소비 신규 doc_type 누락 보완(원시 코드 표시·바로가기 부재)
  EXPENSE: { ko: "경비 청구", href: "/finance-expenses" },
  GENERAL_DRAFT: { ko: "기안문서", href: "/approvals-drafts" },
};

interface LedgerVerify { ok: boolean; entries: number; head_hash?: string;
  first_broken_seq?: number; reason?: string }
interface SignEntry { eventType: string; occurredAt: string; entryHash: string;
  data?: { targetRef?: string; seq_no?: number; entry_count?: number; head_hash?: string } }
interface SignLog { available: boolean; reason?: string; entries: SignEntry[];
  verify: { valid: boolean; chainValid: boolean; anchored: boolean;
    anchoredAt: string | null } | null }

interface VoucherHead { id: number; voucher_on: string; source: string | null; source_ref: string;
  status: string; memo: string | null; amount: number; created_by: number | null }
interface VoucherLineRow { account_code: string | null; side: string | null; amount: number;
  detail: string | null; cost_center: string | null }

/** 전표 결재 — 결재선 단계 적격자(부서장·원장 등)는 전표 원장(/finance) 권한이 없어, 바로가기로는
 *  승인 대상 전표를 열 수도 승인할 수도 없었다(2026-10-09 메뉴·권한 정합). 서버가 **그 전표 한 건**의
 *  머리·분개를 단계 적격자에게 열어 주므로 여기서 내용을 보고 승인/반려한다. */
function VoucherReview({ id, onClose }: { id: number; onClose: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [reason, setReason] = useState("");
  const head = useQuery({ queryKey: ["appr-voucher", id], retry: 0,
    queryFn: () => api<VoucherHead>(`/finance/vouchers/${id}`) });
  const lines = useQuery({ queryKey: ["appr-voucher-lines", id], retry: 0,
    queryFn: () => api<{ draft: boolean; items: VoucherLineRow[] }>(`/finance/vouchers/${id}/lines`) });
  const done = (msg: string) => { toast.ok(msg); qc.invalidateQueries({ queryKey: ["approval-worklist"] });
    onClose(); };
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const approve = useMutation({
    mutationFn: () => api<{ status: string }>(`/finance/vouchers/${id}/approve`, { method: "POST" }),
    onSuccess: (r) => done(r.status === "POSTED" ? "승인 — 전기 완료" : "승인 — 다음 단계로"),
    onError: fail });
  const reject = useMutation({
    mutationFn: () => api(`/finance/vouchers/${id}/reject`, { method: "POST",
      body: JSON.stringify({ reason }) }),
    onSuccess: () => done("반려"), onError: fail });
  const busy = approve.isPending || reject.isPending;
  return (
    <Modal title={`회계 전표 #${id}`} size="lg" dirty={reason.trim() !== ""} onClose={onClose}>
      <QueryErrorBanner queries={[head, lines]} />
      {head.data && (
        <dl className="mb-3 grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-4">
          <div><dt className="text-[11px] text-slate-400">전표일</dt><dd>{head.data.voucher_on}</dd></div>
          <div><dt className="text-[11px] text-slate-400">상태</dt><dd><Badge tone="amber">{head.data.status}</Badge></dd></div>
          <div><dt className="text-[11px] text-slate-400">금액(차변)</dt><dd className="font-semibold">{won(head.data.amount)}</dd></div>
          <div><dt className="text-[11px] text-slate-400">출처</dt><dd className="break-all">{head.data.source ?? "—"}</dd></div>
          {head.data.memo && <div className="col-span-full"><dt className="text-[11px] text-slate-400">적요</dt>
            <dd>{head.data.memo}</dd></div>}
        </dl>)}
      <Table headers={["계정", "차/대", "금액", "적요", "코스트센터"]}
        rows={(lines.data?.items ?? []).map((ln) => [
          ln.account_code ?? "—", ln.side === "DEBIT" ? "차변" : "대변", won(ln.amount),
          ln.detail ?? "", ln.cost_center ?? ""])}
        empty={lines.isFetching ? "불러오는 중…" : "분개 없음"} />
      {lines.data?.draft && <p className="mt-1 text-[11px] text-slate-400">미전기 분개안(승인 완료 시 전기)</p>}
      <div className="mt-3 flex flex-wrap items-end gap-2">
        <Button disabled={busy || !head.data} onClick={() => approve.mutate()}>승인</Button>
        <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="반려 사유(필수)"
          className="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        <Button variant="danger" disabled={busy || !head.data || !reason.trim()}
          onClick={() => reject.mutate()}>반려</Button>
      </div>
    </Modal>
  );
}

export default function ApprovalsPage() {
  const [voucherId, setVoucherId] = useState<number | null>(null);
  const q = useQuery({
    queryKey: ["approval-worklist"],
    queryFn: () => api<{ items: Item[] }>("/approvals/worklist"),
    refetchInterval: 60_000,
  });
  const items = q.data?.items ?? [];
  // 결재 불변 원장(해시체인) 검증 — 감사 성격이라 DIRECTOR/ADMIN만(admin.audit read)
  const role = auth.claims()?.role ?? "STAFF";
  const canVerify = ["DIRECTOR", "ADMIN"].includes(role);
  const chain = useQuery({
    queryKey: ["approval-ledger-verify"], enabled: canVerify, retry: 0,
    queryFn: () => api<LedgerVerify>("/approvals/ledger/verify"),
  });
  // sign 블록체인 시점봉인 이력(B안) — ERP 원장과 독립된 외부 증명(TSA)
  const signLog = useQuery({
    queryKey: ["approval-ledger-sign-log"], enabled: canVerify, retry: 0,
    queryFn: () => api<SignLog>("/approvals/ledger/sign-log"),
  });
  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">자금결재함 — 내 결재 대기</h1>
      <p className="text-xs text-slate-500">
        지출·전표·구매·계약 등 <b>자금 관련 ERP 내부 결재</b>의 내 대기 건입니다(그룹웨어 전자결재와
        별개). 승인/반려는 각 문서 화면에서 처리합니다(회계 전표는 이 화면에서 바로 · 작성자 본인 건 제외).</p>

      {q.isError && <QueryError error={q.error} onRetry={() => q.refetch()} />}

      <Card title={`대기 ${items.length}건`}>
        <Table headers={["유형", "문서#", "금액", "단계", "바로가기"]}
          rows={items.map((it) => {
            const d = DOC[it.doc_type];
            return [
              <Badge key="t" tone="amber">{d?.ko ?? it.doc_type}</Badge>,
              `#${it.doc_id}`,
              it.amount != null ? it.amount.toLocaleString() : "—",
              `${it.step_no}단계`,
              it.doc_type === "VOUCHER"
                ? <button key="g" type="button" onClick={() => setVoucherId(it.doc_id)}
                    className="text-blue-600 hover:underline">전표 보기·결재</button>
                : d ? <Link key="g" href={d.href}
                    className="text-blue-600 hover:underline">결재 화면 ↗</Link>
                : "—",
            ];
          })} empty="대기 중인 결재가 없습니다" />
      </Card>

      {voucherId != null && <VoucherReview id={voucherId} onClose={() => setVoucherId(null)} />}

      {canVerify && !chain.isError && chain.data && (
        <Card title="결재 원장 무결성 (블록체인식 해시체인)">
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {chain.data.ok
              ? <Badge tone="green">체인 무결 ✓</Badge>
              : <Badge tone="red">변조 감지 — seq {chain.data.first_broken_seq}
                  ({chain.data.reason})</Badge>}
            <span className="text-slate-500">기록 {chain.data.entries.toLocaleString()}건</span>
            {chain.data.ok && chain.data.head_hash && (
              <code className="text-[10px] text-slate-400"
                title="체인 head 해시 — 외부 공증/대사용">
                head {chain.data.head_hash.slice(0, 16)}…</code>)}
          </div>
          <p className="mt-2 text-xs text-slate-400">
            모든 결재 행위(상신·승인·반려·전권)는 SHA-256 해시체인 불변 원장에 기록됩니다
            (DB 계층 append-only + 전 구간 재계산 검증 — 관리자 변조도 사후 탐지).</p>
        </Card>
      )}

      {canVerify && signLog.data && (
        <Card title="sign 블록체인 시점봉인 (외부 증명)">
          {signLog.data.available ? (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-3 text-sm">
                {signLog.data.verify?.valid
                  ? <Badge tone="green">sign 체인·TSA 앵커 유효 ✓</Badge>
                  : signLog.data.verify?.chainValid
                    ? <Badge tone="amber">sign 체인 무결 (TSA 앵커 대기)</Badge>
                    : <Badge tone="red">sign 체인 이상</Badge>}
                {signLog.data.verify?.anchoredAt &&
                  <span className="text-xs text-slate-500">
                    최근 봉인 {signLog.data.verify.anchoredAt.slice(0, 19).replace("T", " ")}</span>}
              </div>
              <p className="mb-2 text-xs text-slate-400">
                일일 원장 검증 통과 시 앵커 head를 sign 해시체인에 제출하고 TSA로 시점봉인합니다 —
                ERP와 독립된 외부 시점 증거. 같은 잡이 매일 봉인된 head를 현재 원장과 대조해
                불일치·봉인 지연 시 경보합니다.</p>
              <Table headers={["봉인 시각", "앵커", "봉인된 head 해시", "인증 해시(entryHash)"]}
                rows={(signLog.data.entries ?? []).map((e) => [
                  (e.occurredAt ?? "").slice(0, 19).replace("T", " "),
                  e.data?.targetRef ?? "—",
                  // 대조에 실제로 필요한 값 — 감사인이 원장과 맞춰볼 수 있어야 증거로 기능한다
                  <code key="hh" className="text-[10px] text-slate-500">
                    {(e.data?.head_hash ?? "—").slice(0, 20)}…</code>,
                  <code key="h" className="text-[10px] text-slate-400">
                    {(e.entryHash ?? "").slice(0, 20)}…</code>,
                ])} empty="봉인 이력 없음 (일일 검증 잡이 신규 앵커 생성 시 기록됩니다)" />
            </>
          ) : (
            <p className="text-xs text-slate-500">sign 연동 조회 불가 — {signLog.data.reason}</p>
          )}
        </Card>
      )}
    </div>
  );
}
