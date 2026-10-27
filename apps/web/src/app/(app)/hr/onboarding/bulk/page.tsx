"use client";
/** 인사 — 급여 기초정보 일괄 편집(스프레드시트). 개별 입력(../onboarding)의 대량 편집판. */
import { useCallback, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Sheet } from "@fortune-sheet/core";
import { ApiError, api } from "@/lib/api";
import Spreadsheet, { FILL_HEIGHT, autoColumnlen, cell, sheetToGrid } from "@/components/Spreadsheet";
import { Button, Card } from "@/components/ui";
import { useToast } from "@/components/toast";

interface Row {
  employee_id: number; employee_no: string; name: string; job_category: string | null;
  base_pay: number; dependents: number; bank_name: string | null;
  account_masked: string | null; complete: boolean;
}
const HEADERS = ["사번", "이름", "직종", "기본급(원)", "부양가족", "은행", "계좌(마스킹)", "계좌 변경(입력 시)", "상태"];
const RO_COLS = [0, 1, 2, 6, 8];     // 읽기전용: 사번·이름·직종·계좌마스킹·상태(락 + 회색)
const RO_BG = "#eceff3";             // 읽기전용 셀 배경(별도 표기)
const HDR_BG = "#dbe0e6";

export default function OnboardingBulkPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const gridRef = useRef<(string | number | null)[][] | null>(null);
  const gridKey = useRef("");

  const q = useQuery({
    queryKey: ["onboarding-grid"],
    queryFn: () => api<{ count: number; incomplete: number; items: Row[] }>("/hr/onboarding/grid"),
  });

  // 파생값(state 아님) — q.data 로드 시 1회 갱신, 이후 편집은 gridRef가 추적(setState-in-effect 회피)
  const sheets = useMemo<Sheet[]>(() => {
    const items = q.data?.items ?? [];
    const celldata: { r: number; c: number; v: ReturnType<typeof cell> }[] = [];
    const rowVals: (string | number)[][] = [];
    HEADERS.forEach((h, c) => celldata.push({ r: 0, c, v: cell(h, HDR_BG) }));
    items.forEach((it, i) => {
      const vals: (string | number)[] = [
        it.employee_no, it.name, it.job_category ?? "-", it.base_pay || "",
        it.dependents ?? 1, it.bank_name ?? "", it.account_masked ?? "미등록", "",
        it.complete ? "완료" : "미완료"];
      rowVals.push(vals);
      vals.forEach((v, c) => celldata.push({
        r: i + 1, c, v: RO_COLS.includes(c) ? cell(v, RO_BG) : cell(v) }));   // 읽기전용 열 회색
    });
    return [{ name: "급여 기초정보", celldata,
              row: Math.max(items.length + 8, 80), column: HEADERS.length,
              config: autoColumnlen(HEADERS, rowVals) }];   // 컬럼 폭 자동 배치·뷰포트 채움
  }, [q.data]);
  const idByNo = useMemo(
    () => new Map((q.data?.items ?? []).map((it) => [it.employee_no, it.employee_id])), [q.data]);

  const onChange = useCallback((d: Sheet[]) => {
    const g = sheetToGrid(d[0]);
    const k = JSON.stringify(g);
    if (k === gridKey.current) return;   // 동일 내용 재발화 무한루프 방지
    gridKey.current = k;
    gridRef.current = g;
  }, []);

  const save = useMutation({
    mutationFn: () => {
      const grid = gridRef.current ?? sheetToGrid(sheets[0]);   // 미편집 시 로드값 사용
      const rows = grid.slice(1).map((row) => {                 // 헤더 제외
        const no = String(row?.[0] ?? "").trim();
        const id = idByNo.get(no);
        if (!id) return null;                                 // 사번 미매칭 행 제외(식별키 보호)
        const acct = String(row?.[7] ?? "").trim();   // 계좌 변경 열 — 입력 시에만 변경(마스킹은 무시)
        return {
          employee_id: id,
          base_pay: Number(row?.[3]) || 0,
          dependents: Number(row?.[4]) || 1,
          bank_name: String(row?.[5] ?? "").trim() || null,
          bank_account: acct && !acct.includes("*") ? acct : null,   // 마스킹/공백=기존 유지
        };
      }).filter((r): r is NonNullable<typeof r> => r !== null);
      return api<{ saved: number; skipped: number; errors: unknown[] }>(
        "/hr/onboarding/grid", { method: "POST", body: JSON.stringify({ rows }) });
    },
    onSuccess: (r) => {
      toast.ok(`저장 ${r.saved}건 · 건너뜀 ${r.skipped}건${r.errors.length ? ` · 오류 ${r.errors.length}건` : ""}`);
      qc.invalidateQueries({ queryKey: ["onboarding-grid"] });
      qc.invalidateQueries({ queryKey: ["onboarding"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">인사 — 급여 기초정보 일괄 편집</h1>
        <div className="flex gap-2">
          <Link href="/hr/onboarding"
                className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50">
            ← 개별 입력</Link>
          <Button isLoading={save.isPending} onClick={() => save.mutate()}>일괄 저장</Button>
        </div>
      </div>
      <p className="text-sm text-slate-500">
        재직 직원 <b>전체</b>의 기본급·부양가족·은행·계좌를 스프레드시트로 편집한 뒤 <b>일괄 저장</b>합니다.
        <b>회색 열(사번·이름·직종·계좌 마스킹·상태)은 읽기전용</b>입니다. 계좌는 보안상 <b>마스킹으로만 표시</b>되며,
        변경하려면 <b>[계좌 변경]</b> 열에 새 계좌번호를 입력하세요(비우면 기존 유지·암호화 저장). 기본급 0 이하 행은 건너뜁니다.</p>
      {q.isError && <p className="text-sm text-red-600">조회 권한이 없습니다 (hr.compensation)</p>}
      <Card title={`급여 기초정보 — 전체 ${q.data?.count ?? 0}명 (미완료 ${q.data?.incomplete ?? 0}명)`}
        action={<span className="text-[11px] text-slate-400">회색 열(사번·이름·직종·상태)은 읽기전용</span>}>
        <Spreadsheet data={sheets} height={FILL_HEIGHT} onChange={onChange} readOnlyCols={RO_COLS} />
      </Card>
    </div>
  );
}
