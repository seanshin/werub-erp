"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmProvider } from "@/components/confirm";
import { ToastProvider } from "@/components/toast";

export default function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({
    defaultOptions: {
      queries: {
        retry: 1,
        staleTime: 30_000,
        refetchOnWindowFocus: false,       // 탭 복귀마다 전 쿼리 재조회 방지
        refetchIntervalInBackground: false, // 비활성 탭 백그라운드 폴링 방지
      },
    },
  }));
  return (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <ConfirmProvider>{children}</ConfirmProvider>
      </ToastProvider>
    </QueryClientProvider>
  );
}
