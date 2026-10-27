"use client";
/** IT 자산 QR 실사(ITAM M4) — 태그 스캔/입력 → 자산 확인 → 위치·상태 갱신(현장 실사). */
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { CameraScanButton } from "@/components/camera-scan";
import { catKo } from "@/lib/itam";

interface Asset { id: number; asset_tag: string; name: string; category: string; status: string;
  location: string | null; assigned_employee_id: number | null;
  assigned_employee_name?: string | null; handles_phi: boolean }
interface StaleAsset { id: number; asset_tag: string; name: string; category: string;
  status: string; location: string | null; last_audited_at: string | null }

const STATUS_KO: Record<string, string> = {
  IN_STOCK: "재고", ASSIGNED: "배정", REPAIR: "수리", LOANER: "대여",
  LOST: "분실", STOLEN: "도난", RETIRED: "불용", DISPOSED: "폐기" };

export default function AuditPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [tag, setTag] = useState("");
  const [asset, setAsset] = useState<Asset | null>(null);
  const [location, setLocation] = useState("");
  const [scanned, setScanned] = useState(0);
  const [showReport, setShowReport] = useState(false);
  const report = useQuery({ queryKey: ["itam-audit-report"], enabled: showReport,
    queryFn: () => api<{ days: number; total_active: number; unaudited: number; items: StaleAsset[] }>(
      "/itam/audit-report?days=180") });

  const lookup = useMutation({
    mutationFn: (t: string) => api<{ found: boolean; asset: Asset | null }>(
      `/itam/assets/lookup?tag=${encodeURIComponent(t)}`),
    onSuccess: (r) => {
      if (r.found && r.asset) { setAsset(r.asset); setLocation(r.asset.location ?? ""); }
      else { setAsset(null); toast.err("미등록 태그입니다"); }
    },
    onError: () => toast.err("조회 실패(권한 확인)"),
  });
  const save = useMutation({
    mutationFn: () => api(`/itam/assets/${asset!.id}/audit-stamp`, { method: "POST",
      body: JSON.stringify({ location: location || null }) }),
    onSuccess: () => { toast.ok(`${asset!.asset_tag} 실사 확인·위치 갱신`); setScanned((n) => n + 1);
      setAsset(null); setTag(""); qc.invalidateQueries({ queryKey: ["itam-audit-report"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "실사 확인 실패"),
  });

  const submit = (t: string) => { const v = t.trim(); if (v) lookup.mutate(v); };

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-bold text-slate-900">IT 자산 실사 (QR 스캔)</h1>
          <p className="text-xs text-slate-500">태그를 스캔·입력해 자산을 확인하고 위치를 갱신합니다. 세션 확인 {scanned}건</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setShowReport((v) => !v)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50">
            {showReport ? "실사 화면" : "미실사 리포트"}</button>
          <Link href="/assets/it"
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50">← 목록</Link>
        </div>
      </div>

      {showReport && (
        <Card title={`미실사 자산 — 최근 180일 실사 기록 없음 (${report.data?.unaudited ?? 0} / 운영 ${report.data?.total_active ?? 0})`}>
          <Table headers={["태그", "이름", "분류", "상태", "위치", "최근 실사"]}
            empty="모든 운영 자산이 실사 기준을 충족합니다"
            rows={(report.data?.items ?? []).map((a) => [
              <span key="t" className="font-mono text-xs">{a.asset_tag}</span>,
              a.name, catKo(a.category), STATUS_KO[a.status] ?? a.status, a.location ?? "-",
              a.last_audited_at
                ? <span key="d" className="text-xs">{a.last_audited_at.slice(0, 10)}</span>
                : <Badge key="d" tone="red">실사 이력 없음</Badge>])} />
        </Card>
      )}

      <Card title="태그 스캔 / 입력">
        <div className="flex items-stretch gap-2">
          <input value={tag} autoFocus onChange={(e) => setTag(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(tag); }}
            placeholder="자산 태그 스캔 또는 입력 후 Enter"
            className="min-w-0 flex-1 rounded-xl border-2 border-blue-200 bg-blue-50/40 px-4 py-3 text-base outline-none focus:border-blue-400" />
          <CameraScanButton onScan={(c) => { setTag(c); submit(c); }} />
        </div>
      </Card>

      {asset && (
        <Card title={`${asset.asset_tag} · ${asset.name}`}>
          <div className="space-y-2 text-sm text-slate-700">
            <p><b>상태</b> {STATUS_KO[asset.status] ?? asset.status}
              {asset.handles_phi && <Badge tone="red">PHI</Badge>}
              · <b>분류</b> {catKo(asset.category)}
              · <b>사용자</b> {asset.assigned_employee_name ?? (asset.assigned_employee_id ? `#${asset.assigned_employee_id}` : "-")}</p>
            <Input label="위치(실사 갱신)" value={location} onChange={(e) => setLocation(e.target.value)} />
            <Button isLoading={save.isPending} onClick={() => save.mutate()}>위치 확인·갱신</Button>
          </div>
        </Card>
      )}
    </div>
  );
}
