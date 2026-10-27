"use client";
/** In-app 알림 센터 (FR-26) — 읽음/미읽음 관리 + 딥링크(X2-3). */
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import type { Notification } from "@/types";
import { dt } from "@/lib/format";

const TONE: Record<string, "red" | "amber" | "blue" | "gray"> = {
  LICENSE_EXPIRY: "amber", STOCK: "red", APPEAL_DEADLINE: "red", APPROVAL: "blue",
};

export default function NotificationsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const { data } = useQuery({
    queryKey: ["notifications-full"],
    queryFn: () => api<{ items: Notification[] }>("/notifications?size=100"),
  });
  const markRead = useMutation({
    mutationFn: (id: number) => api(`/notifications/${id}/read`, { method: "PATCH" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notifications-full"] });
      qc.invalidateQueries({ queryKey: ["notifications"] });
    },
    onError: () => toast.err("읽음 처리에 실패했습니다"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">알림 센터</h1>
      <Card>
        <Table headers={["유형", "제목", "내용", "수신", ""]}
          rows={(data?.items ?? []).map((n) => [
            <Badge key="t" tone={TONE[n.type] ?? "gray"}>{n.type}</Badge>,
            n.href
              ? <Link key="ti" href={n.href}
                  className={`text-blue-600 hover:underline ${n.read_at ? "opacity-60" : "font-medium"}`}>{n.title} ↗</Link>
              : <span key="ti" className={n.read_at ? "text-slate-400" : "font-medium"}>{n.title}</span>,
            <span key="b" className="text-slate-500">{n.body ?? "-"}</span>,
            dt(n.created_at),
            !n.read_at &&
              <Button key="r" variant="secondary" onClick={() => markRead.mutate(n.id)}>읽음</Button>,
          ])} />
      </Card>
    </div>
  );
}
