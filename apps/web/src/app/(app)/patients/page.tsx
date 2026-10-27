"use client";
/** 원무 — 환자 검색/등록(FR-15/33)·접수(FR-16)·수납·일마감 (design §7.1). */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { ApiError, api } from "@/lib/api";
import { Button, Card, Input, Table } from "@/components/ui";

interface Patient { id: number; chart_no: string; name: string; birth_date: string; phone: string | null }
interface DeptOpt { id: number; name: string; code: string | null }

// FR-33 — 4필드 스키마 (주민번호 필드 자체가 없음)
const patientSchema = z.object({
  chart_no: z.string().min(1, "차트번호 필수"),
  name: z.string().min(1, "이름 필수"),
  birth_date: z.string().min(1, "생년월일 필수"),
  phone: z.string().regex(/^[\d-]*$/, "숫자·하이픈만").optional().or(z.literal("")),
});
type PatientForm = z.infer<typeof patientSchema>;

export default function PatientsPage() {
  const qc = useQueryClient();
  const [query, setQuery] = useState({ name: "", chart_no: "", phone: "" });
  const [searched, setSearched] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [deptCode, setDeptCode] = useState("");   // 접수 진료과 — 하드코딩 "IM" 제거(데이터 오염 방지)

  // 진료과 목록 — code가 있는 임상과만(비임상 부서는 접수 대상 아님). /departments read = 매니저+,
  // 접수(patient.patients write)=ADMIN이라 접수 권한자는 이 목록 조회 권한을 함께 보유.
  const depts = useQuery({
    queryKey: ["departments"],
    queryFn: () => api<{ items: DeptOpt[] }>("/departments"),
  });
  const clinicalDepts = (depts.data?.items ?? []).filter((d) => d.code);

  const search = useQuery({
    queryKey: ["patients", query],
    // POST 검색(Phase0 W1) — 성명·차트·전화가 쿼리스트링으로 access log에 남지 않도록
    queryFn: () => api<{ items: Patient[] }>("/patients/search", {
      method: "POST",
      body: JSON.stringify(Object.fromEntries(
        Object.entries(query).filter(([, v]) => v))),
    }),
    enabled: searched,
  });

  const createForm = useForm<PatientForm>({ resolver: zodResolver(patientSchema) });
  const createPatient = useMutation({
    mutationFn: (body: object) => api("/patients", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => { setMessage("환자 등록 완료"); qc.invalidateQueries({ queryKey: ["patients"] }); },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "등록 실패"),
  });

  const receive = useMutation({
    mutationFn: (patient_id: number) => api("/patients/receptions", {
      method: "POST",
      body: JSON.stringify({ patient_id, dept_code: deptCode,
                             received_on: todayLocal() }),
    }),
    onSuccess: () => setMessage("접수 완료"),
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "접수 실패"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">원무 — 환자·접수</h1>
      {message && <p className="text-sm text-blue-600">{message}</p>}

      <Card title="환자 검색 (이름/차트번호/전화 — 전화는 전체 일치)">
        <div className="flex flex-wrap items-end gap-2">
          <Input label="이름" value={query.name}
            onChange={(e) => setQuery({ ...query, name: e.target.value })} />
          <Input label="차트번호" value={query.chart_no}
            onChange={(e) => setQuery({ ...query, chart_no: e.target.value })} />
          <Input label="전화번호" value={query.phone}
            onChange={(e) => setQuery({ ...query, phone: e.target.value })} />
          <Button onClick={() => setSearched(true)}>검색</Button>
        </div>
        {searched && (
          <div className="mt-3 space-y-2">
            <div className="flex flex-wrap items-end gap-2">
              <label className="text-sm">
                <span className="mb-1 block text-slate-600">접수 진료과 *</span>
                <select value={deptCode} onChange={(e) => setDeptCode(e.target.value)}
                  className="w-48 rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                  <option value="">진료과 선택</option>
                  {clinicalDepts.map((d) => (
                    <option key={d.id} value={d.code!}>{d.name} ({d.code})</option>
                  ))}
                </select>
              </label>
              {!deptCode && <span className="text-xs text-amber-600">접수하려면 진료과를 먼저 선택하세요</span>}
            </div>
            <Table headers={["차트번호", "이름", "생년월일", "전화(마스킹)", ""]}
              rows={(search.data?.items ?? []).map((p) => [
                p.chart_no, p.name, p.birth_date, p.phone ?? "-",
                <Button key="r" variant="secondary" disabled={!deptCode || receive.isPending}
                  onClick={() => receive.mutate(p.id)}>접수</Button>,
              ])} />
          </div>
        )}
      </Card>

      <Card title="신규 환자 등록 (4필드 최소화 — 주민번호 입력란 없음)">
        <form className="flex flex-wrap items-end gap-2"
          onSubmit={createForm.handleSubmit((f) => createPatient.mutate(f))}>
          <Input label="차트번호" {...createForm.register("chart_no")} />
          <Input label="이름" {...createForm.register("name")} />
          <Input label="생년월일" type="date" {...createForm.register("birth_date")} />
          <Input label="전화 (선택)" {...createForm.register("phone")} />
          <Button type="submit" disabled={createPatient.isPending}>등록</Button>
          {Object.values(createForm.formState.errors).map((e, i) =>
            <p key={i} className="w-full text-xs text-red-600">{e?.message as string}</p>)}
        </form>
      </Card>
    </div>
  );
}
