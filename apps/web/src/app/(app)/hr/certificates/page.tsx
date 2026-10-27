"use client";
/** 증명서 발급 (FR-23) — 발급 기록 + 이력 조회 + PDF 다운로드·재발급. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiDownload } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

interface Cert { id: number; cert_type: string; purpose: string | null; issued_at: string }

const KIND: Record<string, string> = { EMPLOYMENT: "재직증명서", CAREER: "경력증명서" };

export default function CertificatesPage() {
  const qc = useQueryClient();
  const [certType, setCertType] = useState("EMPLOYMENT");
  const [purpose, setPurpose] = useState("");
  const toast = useToast();

  const list = useQuery({ queryKey: ["certs"],
    queryFn: () => api<{ items: Cert[] }>("/hr/certificates") });

  const issue = useMutation({
    mutationFn: () => api<{ id: number }>("/hr/certificates", {
      method: "POST", body: JSON.stringify({ cert_type: certType, purpose }) }),
    onSuccess: (d) => {
      toast.ok(`발급 #${d.id} 완료 — PDF 다운로드를 시작합니다`);
      apiDownload(`/hr/certificates/${d.id}/pdf`, `certificate-${d.id}.pdf`);
      qc.invalidateQueries({ queryKey: ["certs"] });
    },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "발급 실패"),
  });

  return (
    <div className="space-y-4">
      <QueryErrorBanner queries={[list]} />
      <h1 className="text-lg font-bold">인사 — 증명서 발급</h1>

      <Card title="재직·경력증명서 (본인)">
        <div className="flex flex-wrap items-end gap-2">
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">종류</span>
            <select value={certType} onChange={(e) => setCertType(e.target.value)}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm">
              <option value="EMPLOYMENT">재직증명서</option>
              <option value="CAREER">경력증명서</option>
            </select>
          </label>
          <Input label="용도 (선택)" value={purpose}
            onChange={(e) => setPurpose(e.target.value)} className="min-w-56" />
          <Button onClick={() => issue.mutate()} disabled={issue.isPending}>발급 + PDF</Button>
        </div>
      </Card>

      <Card title="발급 이력">
        <Table headers={["발급번호", "종류", "용도", "발급일시", ""]}
          rows={(list.data?.items ?? []).map((c) => [
            `CERT-${String(c.id).padStart(6, "0")}`,
            <Badge key="k" tone="blue">{KIND[c.cert_type] ?? c.cert_type}</Badge>,
            c.purpose ?? "—",
            c.issued_at?.slice(0, 16).replace("T", " "),
            <Button key="d" variant="neutral" size="sm" onClick={() => apiDownload(`/hr/certificates/${c.id}/pdf`, `certificate-${c.id}.pdf`)}>PDF 재발급</Button>,
          ])} />
      </Card>
    </div>
  );
}
