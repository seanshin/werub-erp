"use client";
/** IT 자산 일괄 관리(스프레드시트) — /assets/it 개별 화면의 대량 편집판. asset_tag 키로 upsert. */
import { useCallback, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Sheet } from "@fortune-sheet/core";
import { ApiError, api } from "@/lib/api";
import Spreadsheet, { FILL_HEIGHT, autoColumnlen, cell, sheetToGrid } from "@/components/Spreadsheet";
import { Button, Card } from "@/components/ui";
import { useToast } from "@/components/toast";

interface Asset {
  asset_tag: string; name: string; category: string; serial_no: string | null;
  manufacturer: string | null; model: string | null; status: string;
  location: string | null; warranty_until: string | null; ip_addr: string | null;
  hostname: string | null; handles_phi: boolean;
}
const HEADERS = ["태그", "이름", "분류", "시리얼", "제조사", "모델", "상태",
  "위치", "보증만기(YYYY-MM-DD)", "IP", "호스트명", "PHI(Y)"];
const RO_COLS = [0, 3];        // 읽기전용: 태그(식별키)·시리얼(물리 식별) — 기존 행만 락 + 회색
const RO_BG = "#eceff3";       // 읽기전용 셀 배경(별도 표기)
const HDR_BG = "#dbe0e6";
const MIN_ROWS = 80;           // 확장된 높이를 채우는 빈 행(신규 입력 공간)

export default function ITAssetBulkPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const gridRef = useRef<(string | number | null)[][] | null>(null);
  const gridKey = useRef("");

  const q = useQuery({ queryKey: ["itam-assets-grid"],
    queryFn: () => api<{ items: Asset[] }>("/itam/assets") });
  const itemCount = q.data?.items.length ?? 0;

  const sheets = useMemo<Sheet[]>(() => {
    const items = q.data?.items ?? [];
    const celldata: { r: number; c: number; v: ReturnType<typeof cell> }[] = [];
    const rowVals: (string | number)[][] = [];
    HEADERS.forEach((h, c) => celldata.push({ r: 0, c, v: cell(h, HDR_BG) }));
    items.forEach((a, i) => {
      const vals: (string | number)[] = [
        a.asset_tag, a.name, a.category, a.serial_no ?? "", a.manufacturer ?? "",
        a.model ?? "", a.status, a.location ?? "", a.warranty_until ?? "",
        a.ip_addr ?? "", a.hostname ?? "", a.handles_phi ? "Y" : ""];
      rowVals.push(vals);
      vals.forEach((v, c) => celldata.push({
        r: i + 1, c, v: RO_COLS.includes(c) ? cell(v, RO_BG) : cell(v) }));   // 기존 행 식별자 회색
    });
    return [{ name: "IT 자산", celldata,
              row: Math.max(items.length + 12, MIN_ROWS), column: HEADERS.length,
              config: autoColumnlen(HEADERS, rowVals) }];   // 컬럼 폭 자동 배치(공용)
  }, [q.data]);

  const onChange = useCallback((d: Sheet[]) => {
    const g = sheetToGrid(d[0]);
    const k = JSON.stringify(g);
    if (k === gridKey.current) return;
    gridKey.current = k;
    gridRef.current = g;
  }, []);

  const save = useMutation({
    mutationFn: () => {
      const grid = gridRef.current ?? sheetToGrid(sheets[0]);
      const rows = grid.slice(1).map((row) => {
        const tag = String(row?.[0] ?? "").trim();
        if (!tag) return null;                          // 태그 없는 행 제외
        const phi = String(row?.[11] ?? "").trim().toUpperCase();
        const s = (i: number) => String(row?.[i] ?? "").trim() || null;
        return {
          asset_tag: tag, name: s(1), category: s(2), serial_no: s(3),
          manufacturer: s(4), model: s(5), status: s(6), location: s(7),
          warranty_until: s(8), ip_addr: s(9), hostname: s(10),
          handles_phi: phi === "Y" ? true : phi === "N" ? false : null,
        };
      }).filter((r): r is NonNullable<typeof r> => r !== null);
      return api<{ created: number; updated: number; skipped: number; errors: unknown[] }>(
        "/itam/assets/bulk", { method: "POST", body: JSON.stringify({ rows }) });
    },
    onSuccess: (r) => {
      toast.ok(`신규 ${r.created} · 갱신 ${r.updated} · 건너뜀 ${r.skipped}` +
        (r.errors.length ? ` · 오류 ${r.errors.length}` : ""));
      qc.invalidateQueries({ queryKey: ["itam-assets-grid"] });
      qc.invalidateQueries({ queryKey: ["itam-assets"] });
    },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패"),
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">IT 자산 — 일괄 관리(스프레드시트)</h1>
        <div className="flex gap-2">
          <Link href="/assets/it"
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-50">← 개별 화면</Link>
          <Button isLoading={save.isPending} onClick={() => save.mutate()}>일괄 저장</Button>
        </div>
      </div>
      <p className="text-sm text-slate-500">
        등록된 자산을 <b>일괄 수정</b>하고, <b>하단 빈 행에 태그·이름을 입력하면 신규 등록</b>됩니다(태그 키 upsert).
        기존 행의 <b>회색 열(태그·시리얼)은 식별자라 잠겨</b> 수정되지 않습니다(신규 행에서는 입력 가능). 상태는
        <span className="font-mono text-xs"> IN_STOCK·ASSIGNED·REPAIR·LOANER·LOST·STOLEN·RETIRED·DISPOSED</span>,
        PHI는 <b>Y</b>(취급)/<b>N</b>(해제)/공백(변경 없음). 도입일·사용자·사양까지 함께 넣으려면 [+ 자산 등록] 사용.</p>
      {q.isError && <p className="text-sm text-red-600">조회 권한이 없습니다 (itam.assets)</p>}
      <Card title={`IT 자산 — 전체 ${itemCount}건`}
        action={<span className="text-[11px] text-slate-400">회색(태그·시리얼)=기존 행 읽기전용 · 빈 행=신규 입력</span>}>
        <Spreadsheet data={sheets} height={FILL_HEIGHT} onChange={onChange}
          readOnlyCols={RO_COLS} readOnlyRowLimit={itemCount} />
      </Card>
    </div>
  );
}
