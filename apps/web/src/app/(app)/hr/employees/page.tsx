"use client";
/** 인사 — 직원 목록(의료진/일반직 분리·고용형태 필터) + 상세(발령/면허) + 명세 자동생성.
 *  의료기관 특성: 직군(MEDICAL/SUPPORT)·고용형태·인사 SoT(HIS 미러/ERP 관리) 구분. */
import { monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, apiDownload, ApiError } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { AttachmentCell } from "@/components/attachment-cell";
import type { Employee } from "@/types";
interface Detail extends Employee {
  appointments: { type: string; from_dept: number | null; to_dept: number | null;
                  to_position: string | null; effective_on: string }[];
  licenses: { type: string; no: string; expires_on: string | null }[];
}

const CLASS_TABS = [
  { key: "", label: "전체" },
  { key: "MEDICAL", label: "의료진" },
  { key: "SUPPORT", label: "일반직" },
];
const EMP_TYPES = ["", "REGULAR", "CONTRACT", "DAILY", "TEMP", "WATCH"];
const EMP_LABEL: Record<string, string> = { REGULAR: "정규직", CONTRACT: "계약직",
  DAILY: "일용직", TEMP: "임시직", WATCH: "감시단속(경비)" };

export default function EmployeesPage() {
  const [detailId, setDetailId] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const [klass, setKlass] = useState("");
  const [etype, setEtype] = useState("");
  const [yyyymm, setYyyymm] = useState(() => monthLocal());
  const toast = useToast();

  const params = new URLSearchParams({ size: "100" });
  if (q) params.set("q", q);
  if (klass) params.set("staff_class", klass);
  if (etype) params.set("employment_type", etype);
  const { data } = useQuery({
    queryKey: ["employees", q, klass, etype],
    queryFn: () => api<{ items: Employee[] }>(`/hr/employees?${params.toString()}`),
  });
  const detail = useQuery({
    queryKey: ["employee-detail", detailId],
    queryFn: () => api<Detail>(`/hr/employees/${detailId}`),
    enabled: detailId !== null,
  });

  const gen = useMutation({
    mutationFn: (employee_id: number) => api<{ gross: number; net: number; deduction_total: number }>(
      `/hr/payrolls/${yyyymm}/generate`,
      { method: "POST", body: JSON.stringify({ employee_id }) }),
    onSuccess: (r) => toast.ok(`명세 생성 — 지급 ${r.gross.toLocaleString()} · 공제 ${r.deduction_total.toLocaleString()} · 실수령 ${r.net.toLocaleString()}`),
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "명세 생성 실패"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">인사 — 직원 (의료진·일반직 분리)</h1>
        <button onClick={() => apiDownload("/export/employees.xlsx", "employees.xlsx")}
          className="text-sm text-blue-600 hover:underline">Excel 내보내기</button>
      </div>

      {detailId !== null && (
        <div className="rounded-lg border border-blue-200 bg-blue-50/40 p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-semibold text-slate-700">
              직원 상세 {detail.data ? `— ${detail.data.name}` : detail.isLoading ? "(불러오는 중…)" : ""}</span>
            <button onClick={() => setDetailId(null)}
              className="rounded px-2 py-0.5 text-sm text-slate-500 hover:bg-slate-200">✕ 닫기</button>
          </div>
          {detail.isError && <p className="text-sm text-red-600">상세를 불러오지 못했습니다.</p>}
          {detail.data && (
            <div className="grid gap-4 lg:grid-cols-2">
              <Card title="발령 이력">
                <Table headers={["구분", "부서 이동", "직급", "발효일"]}
                  rows={detail.data.appointments.map((a) => [
                    <Badge key="t" tone={a.type === "HIRE" ? "green" : "blue"}>{a.type}</Badge>,
                    `${a.from_dept ?? "-"} → ${a.to_dept ?? "-"}`,
                    a.to_position ?? "-", a.effective_on,
                  ])} empty="발령 이력 없음" />
              </Card>
              <Card title="자격·면허 (만료 D-30/7 자동 알림)">
                <Table headers={["종류", "번호", "만료일"]}
                  rows={detail.data.licenses.map((lc) => [
                    lc.type, lc.no,
                    lc.expires_on
                      ? <Badge key="e" tone={new Date(lc.expires_on) < new Date(Date.now() + 30 * 864e5)
                          ? "red" : "gray"}>{lc.expires_on}</Badge>
                      : "무기한",
                  ])} empty="등록된 면허 없음" />
              </Card>
              <Card title="서류 (계약서·증빙·자격증 사본 등)">
                <div className="flex items-center gap-2 text-sm text-slate-600">
                  <span>📎 첨부 / 다운로드:</span>
                  <AttachmentCell entityType="employee" entityId={detailId} />
                </div>
                <p className="mt-2 text-[11px] text-slate-400">PDF·이미지·문서, 파일당 20MB. 동일 파일 자동 중복제거.</p>
              </Card>
            </div>
          )}
        </div>
      )}

      <Card>
        {/* 직군 탭 */}
        <div className="mb-3 flex gap-1 border-b border-slate-200">
          {CLASS_TABS.map((t) => (
            <button key={t.key} onClick={() => setKlass(t.key)}
              className={`px-4 py-2 text-sm font-medium -mb-px border-b-2 ${
                klass === t.key ? "border-blue-600 text-blue-700" : "border-transparent text-slate-500"}`}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="이름·사번 검색"
            className="w-56 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <select value={etype} onChange={(e) => setEtype(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {EMP_TYPES.map((t) => <option key={t} value={t}>{t ? EMP_LABEL[t] : "고용형태 전체"}</option>)}
          </select>
          <input value={yyyymm} onChange={(e) => setYyyymm(e.target.value)} placeholder="YYYY-MM"
            className="w-24 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <span className="text-xs text-slate-400">{data?.items?.length ?? 0}명</span>
        </div>
        <Table sortable pageSize={25} stickyHeader maxHeight={640}
          headers={["사번", "이름", "관리", "직군", "직종", "고용형태", "부서", "입사일", "명세", ""]}
          rows={(data?.items ?? []).map((e) => [
            e.employee_no, e.name,
            <Badge key="s" tone={e.source === "HIS" ? "blue" : "gray"}>{e.source ?? "ERP"}</Badge>,
            <Badge key="c" tone={e.staff_class === "MEDICAL" ? "green" : "gray"}>
              {e.staff_class === "MEDICAL" ? "의료진" : e.staff_class === "SUPPORT" ? "일반직" : "-"}</Badge>,
            e.job_category ?? e.job_group,
            <Badge key="t" tone={e.employment_type === "REGULAR" ? "gray" : "amber"}>
              {EMP_LABEL[e.employment_type ?? "REGULAR"] ?? e.employment_type}</Badge>,
            e.department_id ?? "-", e.hired_on,
            <Button key="g" variant="secondary" disabled={gen.isPending} onClick={() => gen.mutate(e.id)}>{yyyymm} 생성</Button>,
            <Button key="d" variant="secondary" onClick={() => setDetailId(e.id)}>상세</Button>,
          ])} />
        <p className="mt-2 text-xs text-slate-400">
          관리: HIS=의료진·정규직 미러(읽기전용) / ERP=비정규 직군 직접관리. 명세는 직종·고용형태 규칙으로 자동 산출.
        </p>
      </Card>

      <p className="text-xs text-slate-400">
        조회 범위는 서버가 역할별로 제한합니다 (부서장=본부서, 직원=본인). 조회·반출은 접속기록에 남습니다.
      </p>
    </div>
  );
}
