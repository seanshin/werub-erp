"use client";
/** FortuneSheet 래퍼 — 브라우저 전용(DOM 사용)이라 Next에서 ssr:false 동적 로드.
 *  사용: <Spreadsheet data={[{ name:"Sheet1", celldata:[{r:0,c:0,v:"A1"}] }]} /> */
import dynamic from "next/dynamic";
import type { Sheet } from "@fortune-sheet/core";
import "@fortune-sheet/react/dist/index.css";

const Workbook = dynamic(
  () => import("@fortune-sheet/react").then((m) => m.Workbook),
  { ssr: false, loading: () => <div className="p-4 text-sm text-slate-400">스프레드시트 로딩…</div> },
);

export interface SpreadsheetProps {
  data: Sheet[];
  /** px(number) 또는 CSS 길이(string, 예: "calc(100vh - 320px)") — 뷰포트 채움. */
  height?: number | string;
  onChange?: (data: Sheet[]) => void;
  /** 읽기전용 열(0-base) — 해당 열 셀 편집 거부(beforeUpdateCell 훅). 헤더는 항상 잠금. */
  readOnlyCols?: number[];
  /** 열 잠금을 적용할 데이터 행 상한(1-base 데이터 행 수) — 그 아래 빈 행은 전체 편집 허용
   *  (기존 행 식별자만 잠그고 신규 행 입력은 여는 용도). 미지정 시 전 행 잠금. */
  readOnlyRowLimit?: number;
}

export default function Spreadsheet({ data, height = 480, onChange, readOnlyCols,
                                      readOnlyRowLimit }: SpreadsheetProps) {
  const locked = new Set(readOnlyCols ?? []);
  // hooks는 항상 객체여야 함 — FortuneSheet 내부가 hooks.beforeRenderCellArea 등을 직접 접근하므로
  // undefined 전달 시 "Cannot read properties of undefined" 크래시(readOnlyCols 없는 시트).
  const hooks = readOnlyCols?.length
    ? { beforeUpdateCell: (r: number, c: number) =>
        r !== 0 && (!locked.has(c)
                    || (readOnlyRowLimit !== undefined && r > readOnlyRowLimit)) }  // false=편집 거부
    : {};
  return (
    <div style={{ height, minHeight: 360 }}
      className="overflow-hidden rounded-lg border border-slate-200">
      <Workbook data={data} onChange={onChange} hooks={hooks} />
    </div>
  );
}

/** raw 값 → FortuneSheet Cell 객체 ({v, m}). celldata.v는 Cell 타입이어야 함. */
export function cell(v: string | number, bg?: string): { v: string | number; m: string; bg?: string } {
  return bg ? { v, m: String(v), bg } : { v, m: String(v) };
}

/** 전체 화면 편집기용 뷰포트 채움 높이 — 상단 헤더·안내·툴바 오프셋 제외(하단 빈 공백 제거). */
export const FILL_HEIGHT = "calc(100vh - 330px)";

/** 내용 기준 컬럼 폭(px) 추정 — 한글/전각 ~14px·영숫자 ~8px + 여백. */
export function estWidth(s: string): number {
  let w = 0;
  for (const ch of s) w += ch.charCodeAt(0) > 0x2e80 ? 14 : 8;
  return w + 28;
}

/** 헤더+행 내용으로 컬럼 폭 자동 배치 config(columnlen) 생성 — Sheet에 `config`로 병합. */
export function autoColumnlen(headers: string[],
                             rows: (string | number | null)[][]): { columnlen: Record<string, number> } {
  const maxw = headers.map((h) => estWidth(h));
  rows.forEach((row) => row.forEach((v, c) => {
    if (c < maxw.length && v != null) maxw[c] = Math.max(maxw[c], estWidth(String(v)));
  }));
  const columnlen: Record<string, number> = {};
  maxw.forEach((w, c) => { columnlen[String(c)] = Math.min(320, w); });
  return { columnlen };
}

/** Sheet → 2차원 값 배열(grid). 초기 celldata와 편집 후 data 매트릭스 모두 지원 —
 *  차트 등 외부 소비자가 FortuneSheet 내부 표현에 의존하지 않게 하는 경계. */
export function sheetToGrid(sheet?: Sheet): (string | number | null)[][] {
  if (!sheet) return [];
  const grid: (string | number | null)[][] = [];
  const set = (r: number, c: number, v: string | number) => {
    grid[r] = grid[r] ?? [];
    grid[r][c] = v;
  };
  const cellValue = (v: unknown): string | number | null => {
    if (v === null || v === undefined) return null;
    if (typeof v === "string" || typeof v === "number") return v;
    if (typeof v === "object" && "v" in (v as Record<string, unknown>)) {
      const inner = (v as { v?: unknown }).v;
      return typeof inner === "string" || typeof inner === "number" ? inner : null;
    }
    return null;
  };
  if (sheet.data?.length) {                      // 편집 후 — 셀 매트릭스
    sheet.data.forEach((row, r) => row?.forEach((c0, c) => {
      const v = cellValue(c0);
      if (v !== null) set(r, c, v);
    }));
  } else if (sheet.celldata?.length) {           // 초기 — 희소 celldata
    sheet.celldata.forEach(({ r, c, v }) => {
      const val = cellValue(v);
      if (val !== null) set(r, c, val);
    });
  }
  for (let r = 0; r < grid.length; r++) {
    grid[r] = grid[r] ?? [];
    for (let c = 0; c < grid[r].length; c++) grid[r][c] = grid[r][c] ?? null;
  }
  return grid;
}

/** 행 배열(객체) → FortuneSheet celldata 변환 헬퍼. headers 순서대로 1행 헤더 + 데이터.
 *  rowBg(ri)가 색을 반환하면 해당 데이터 행 전체 하이라이트(적발 라인 강조용). */
export function toSheet(name: string, headers: string[], rows: (string | number | null)[][],
                        rowBg?: (ri: number) => string | undefined): Sheet {
  const celldata: { r: number; c: number; v: ReturnType<typeof cell> }[] = [];
  headers.forEach((h, c) => celldata.push({ r: 0, c, v: cell(h) }));
  rows.forEach((row, ri) => {
    const bg = rowBg?.(ri);
    row.forEach((val, c) => {
      if (val !== null && val !== undefined) celldata.push({ r: ri + 1, c, v: cell(val, bg) });
    });
  });
  return { name, celldata, row: rows.length + 1, column: headers.length,
           config: autoColumnlen(headers, rows) };   // 컬럼 폭 자동 배치
}
