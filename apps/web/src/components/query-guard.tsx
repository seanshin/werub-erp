"use client";
/** 쿼리 상태 가드 — 로딩/에러/데이터를 일관 처리. API 에러(403/500 등)를 "데이터 없음"으로
 * 오인하던 화면(전체검토 P0-6·세무·마감 등 위험)에 에러 배너+재시도를 강제한다.
 *
 * 사용: <QueryGuard q={query}>{(data) => <Table rows={data.items…} />}</QueryGuard>
 */
import type { UseQueryResult } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { Spinner } from "@/components/ui";

export function queryErrorText(e: unknown): string {
  const status = e instanceof ApiError ? e.status : 0;
  if (status === 403) return "이 정보를 볼 권한이 없습니다 (관리자에게 문의).";
  if (status === 401) return "세션이 만료되었습니다. 다시 로그인해 주세요.";
  if (status >= 500) return "서버 오류입니다. 잠시 후 다시 시도해 주세요.";
  if (e instanceof ApiError) return e.message;
  return "데이터를 불러오지 못했습니다 (네트워크 확인).";
}

export function QueryError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
      {queryErrorText(error)}
      {onRetry && (
        <button onClick={onRetry} className="ml-2 underline hover:no-underline">다시 시도</button>
      )}
    </div>
  );
}

/** 다중 쿼리 화면(재무 대시보드 등)용 — 여러 쿼리 중 첫 에러를 배너로. 하나라도 실패하면
 * "빈 데이터/0"으로 오인되지 않도록 상단에 에러+재시도를 노출(전체검토 R4). */
export function QueryErrorBanner({ queries }: { queries: UseQueryResult<unknown>[] }) {
  const failed = queries.find((q) => q.isError);
  if (!failed) return null;
  return <QueryError error={failed.error} onRetry={() => failed.refetch()} />;
}

export function QueryGuard<T>({ q, children }: {
  q: UseQueryResult<T>;
  children: (data: T) => React.ReactNode;
}) {
  if (q.isError) return <QueryError error={q.error} onRetry={() => q.refetch()} />;
  if (q.isPending) return <Spinner label="불러오는 중…" />;
  return <>{children(q.data as T)}</>;
}
