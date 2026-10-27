"use client";
/** 내 IT 자산 · 수령 확인(F-7) — 본인에게 배정된 자산을 직원이 직접 수령 확인(chain-of-custody). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { catKo } from "@/lib/itam";

interface MyAssignment {
  assignment_id: number; asset_id: number; asset_tag: string; name: string;
  category: string; assigned_on: string; acknowledged_at: string | null;
}

export default function MyAssetsPage() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["itam-my-assignments"],
    queryFn: () => api<{ items: MyAssignment[] }>("/itam/my/assignments") });
  const ack = useMutation({
    mutationFn: (id: number) => api(`/itam/my/assignments/${id}/acknowledge`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["itam-my-assignments"] }),
    onError: (e) => alert(e instanceof ApiError ? e.message : "수령 확인 실패"),
  });
  const err = list.error instanceof ApiError ? list.error.message : null;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold text-slate-900">내 IT 자산 · 수령 확인</h1>
        <p className="text-xs text-slate-500">본인에게 배정된 장비를 직접 수령 확인합니다(인수인계 증적).</p>
      </div>
      <Card>
        {err ? <p className="text-sm text-red-600">{err}</p> : (
          <Table headers={["태그", "이름", "분류", "배정일", "수령확인", ""]}
            empty="배정된 자산이 없습니다"
            rows={(list.data?.items ?? []).map((m) => [
              <span key="t" className="font-mono text-xs">{m.asset_tag}</span>,
              m.name, catKo(m.category), m.assigned_on,
              m.acknowledged_at
                ? <Badge key="a" tone="green">확인됨</Badge>
                : <Badge key="a" tone="amber">미확인</Badge>,
              m.acknowledged_at ? "" :
                <Button key="b" isLoading={ack.isPending} onClick={() => ack.mutate(m.assignment_id)}>
                  수령 확인</Button>,
            ])} />
        )}
      </Card>
    </div>
  );
}
