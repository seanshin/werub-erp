"use client";
/** 도구 — 스프레드시트(FortuneSheet) + 차트(Recharts).
 *  코드 마스터 검색결과를 엑셀형 그리드로 조회·편집하고, 하단 차트가 시트와 실시간 연동. */
import { useCallback, useRef, useState } from "react";
import type { Sheet } from "@fortune-sheet/core";
import { api } from "@/lib/api";
import { Card } from "@/components/ui";
import { useToast } from "@/components/toast";
import SheetChart, { type Grid } from "@/components/SheetChart";
import Spreadsheet, { FILL_HEIGHT, sheetToGrid, toSheet } from "@/components/Spreadsheet";

interface FeeRow {
  code: string; code_type: string; name: string; unit_price: number;
  criteria: string | null; valid_from: string;
}
const SAMPLE: Sheet[] = [toSheet("예시",
  ["항목", "1월", "2월", "합계"],
  [["수익", 1000, 1200, 2200], ["비용", 600, 700, 1300], ["이익", 400, 500, 900]])];

export default function SpreadsheetToolPage() {
  const [sheets, setSheets] = useState<Sheet[]>(SAMPLE);
  const [grid, setGrid] = useState<Grid>(() => sheetToGrid(SAMPLE[0]));
  const [q, setQ] = useState("");
  const toast = useToast();

  // FortuneSheet는 마운트·갱신마다 onChange를 발화 — 내용이 같은데 새 배열로 setState하면
  // 리렌더→재발화 무한루프(React #185). 직렬화 키로 실제 변경만 반영 + 핸들러 identity 고정.
  const gridKey = useRef(JSON.stringify(sheetToGrid(SAMPLE[0])));
  const applyGrid = useCallback((g: Grid) => {
    const key = JSON.stringify(g);
    if (key === gridKey.current) return;
    gridKey.current = key;
    setGrid(g);
  }, []);
  const onSheetChange = useCallback(
    (d: Sheet[]) => applyGrid(sheetToGrid(d[0])), [applyGrid]);

  const replaceSheets = (next: Sheet[]) => {
    setSheets(next);
    applyGrid(sheetToGrid(next[0]));
  };

  async function loadCodes() {
    if (!q) return;
    try {
      const res = await api<{ items: FeeRow[] }>(
        `/regulatory/fees/search?q=${encodeURIComponent(q)}&limit=50`);
      const rows = res.items.map((f) => [
        f.code, f.code_type, f.name, f.unit_price, f.criteria ?? "", f.valid_from]);
      replaceSheets([toSheet("코드", ["코드", "종류", "명칭", "단가", "근거", "시행"], rows)]);
      toast.ok(`${res.items.length}건 로드`);
    } catch {
      toast.err("로드 실패");
    }
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">도구 — 스프레드시트 (FortuneSheet)</h1>
      <Card title="코드 마스터 → 스프레드시트">
        <div className="mb-3 flex items-center gap-2">
          <input value={q} onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && loadCodes()}
            placeholder="약가·수가 검색어 (예: 포크랄, 금관)"
            className="w-64 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <button onClick={loadCodes}
            className="rounded-md bg-slate-700 px-3 py-1.5 text-sm text-white">불러오기</button>
          <button onClick={() => replaceSheets(SAMPLE)}
            className="text-sm text-slate-500 hover:underline">예시로 초기화</button>
        </div>
        <Spreadsheet data={sheets} height={FILL_HEIGHT} onChange={onSheetChange} />
        <p className="mt-2 text-xs text-slate-400">
          엑셀형 편집·수식·필터 지원. 검색결과를 그리드로 보거나, 직접 입력해 작업용으로 활용.
        </p>
      </Card>

      <Card title="차트 (Recharts)" action={
        <span className="text-[11px] text-slate-400">시트 데이터 연동 — 셀 편집 시 자동 갱신</span>}>
        <SheetChart grid={grid} />
      </Card>
    </div>
  );
}
