"use client";
/** (app) 세그먼트 에러 바운더리 — 렌더 크래시(예상 못한 null 등)를 사이드바 유지한 채 복구 UI로.
 * 과거엔 크래시 시 Next 기본 빈 화면(복구 경로 없음)이었음(전체검토 P0-5). */
import { useEffect } from "react";

export default function AppError({ error, reset, unstable_retry }: {
  error: Error & { digest?: string };
  reset?: () => void;
  unstable_retry?: () => void;
}) {
  useEffect(() => { console.error("[app error]", error); }, [error]);
  const retry = unstable_retry ?? reset;
  return (
    <div className="mx-auto mt-8 max-w-lg rounded-lg border border-red-200 bg-red-50 p-6 text-center">
      <h2 className="text-lg font-bold text-red-700">화면을 표시하는 중 오류가 발생했습니다</h2>
      <p className="mt-2 text-sm text-slate-600">
        일시적 문제일 수 있습니다. 다시 시도하거나 대시보드로 이동하세요.
      </p>
      {error?.digest && <p className="mt-1 text-xs text-slate-400">오류 코드: {error.digest}</p>}
      <div className="mt-4 flex justify-center gap-2">
        <button onClick={() => retry?.()}
          className="rounded-md bg-blue-600 px-4 py-1.5 text-sm text-white hover:bg-blue-700">
          다시 시도
        </button>
        <a href="/erp/dashboard"
          className="rounded-md border border-slate-300 px-4 py-1.5 text-sm hover:bg-slate-100">
          대시보드
        </a>
      </div>
    </div>
  );
}
