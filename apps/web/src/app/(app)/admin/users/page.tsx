"use client";
/** 사용자 권한 (ERP 내부) — HIS는 ERP 접근(SSO)만, 내부 역할은 ERP 관리자가 별도 배분. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useConfirm } from "@/components/confirm";
import { QueryError } from "@/components/query-guard";

interface U { id: number; email: string; role: string; department_id: number | null;
  employee_id: number | null; is_active: boolean; license_flag_active: boolean;
  email_duplicate?: boolean; relink_candidate?: number | null }

const ROLES = ["DIRECTOR", "ADMIN", "MANAGER", "STAFF",
  "IT_MANAGER", "FINANCE_MANAGER", "LOGISTICS_MANAGER"];
const ROLE_KO: Record<string, string> = {
  DIRECTOR: "원장", ADMIN: "관리자", MANAGER: "부서장", STAFF: "직원",
  IT_MANAGER: "IT관리자", FINANCE_MANAGER: "회계관리자", LOGISTICS_MANAGER: "물류관리자" };

export default function UsersPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const users = useQuery({ queryKey: ["admin-users"],
    queryFn: () => api<{ items: U[] }>("/admin/users") });
  const refresh = () => qc.invalidateQueries({ queryKey: ["admin-users"] });
  // 쓰기(역할·활성·중복 정리·재연결)는 ADMIN만(admin.users write) — DIRECTOR는 열람만(403 확인창 방지)
  const canWrite = auth.claims()?.role === "ADMIN";
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  const role = useMutation({
    mutationFn: (p: { id: number; role: string }) =>
      api(`/admin/users/${p.id}/role`, { method: "PATCH", body: JSON.stringify({ role: p.role }) }),
    onSuccess: () => { toast.ok("역할 변경 완료 — 해당 사용자는 재인증됩니다"); refresh(); },
    onError: fail });
  const retire = useMutation({
    mutationFn: (id: number) => api(`/admin/users/${id}/retire-duplicate`, { method: "POST" }),
    onSuccess: () => { toast.ok("중복 계정을 정리했습니다"); refresh(); }, onError: fail,
  });
  const retireDup = async (u: U) => {
    if (await confirm({ title: "중복 계정 정리", danger: true, confirmLabel: "정리",
      body: `${u.email} 행을 비활성화하고 이메일을 보관용으로 바꿉니다. 대소문자만 다른 중복 계정이 있으면 `
        + "HIS 로그인(SSO)이 거부되므로, 실제 사용하지 않는 쪽을 정리하세요(보통 직원 연결이 없는 쪽)." }))
      retire.mutate(u.id);
  };
  const relink = useMutation({
    mutationFn: (p: { id: number; employee_id: number }) => api(`/admin/users/${p.id}/relink`,
      { method: "POST", body: JSON.stringify({ employee_id: p.employee_id }) }),
    onSuccess: () => { toast.ok("재직 기록으로 재연결하고 활성화했습니다"); refresh(); }, onError: fail,
  });
  const relinkUser = async (u: U) => {
    if (!u.relink_candidate) return;
    if (await confirm({ title: "재직 기록으로 재연결", confirmLabel: "재연결·활성화",
      body: `${u.email}은 퇴사한 직원 #${u.employee_id} 기록에 연결돼 있고, 같은 이메일의 재직 직원 `
        + `#${u.relink_candidate}이 있습니다. 같은 사람(재입사)인지 확인한 뒤에만 실행하세요. `
        + "다른 사람(공용 이메일)이면 그 사람의 급여·연차가 이 계정에 보이게 됩니다." }))
      relink.mutate({ id: u.id, employee_id: u.relink_candidate });
  };
  const active = useMutation({
    mutationFn: (p: { id: number; active: boolean }) =>
      api(`/admin/users/${p.id}/active`, { method: "PATCH",
        body: JSON.stringify({ active: p.active }) }),
    onSuccess: () => { toast.ok("활성 상태 변경 완료"); refresh(); }, onError: fail });

  // 무확인 즉시 PATCH(스크롤 미끄러짐 한 번에 권한 변경+강제 재인증) 방지 — 확인 후 실행(X1-2).
  const changeRole = async (u: U, newRole: string) => {
    if (newRole === u.role) return;
    if (await confirm({ title: "역할 변경", danger: true, confirmLabel: "변경",
      body: `${u.email}의 역할을 '${ROLE_KO[newRole]}'(으)로 변경합니다. 해당 사용자는 즉시 재인증됩니다.` }))
      role.mutate({ id: u.id, role: newRole });
  };
  const toggleActive = async (u: U) => {
    if (await confirm({ title: u.is_active ? "계정 비활성화" : "계정 활성화",
      danger: u.is_active, confirmLabel: u.is_active ? "비활성화" : "활성화",
      body: `${u.email}을(를) ${u.is_active ? "비활성화(로그인 차단)" : "활성화"}합니다.` }))
      active.mutate({ id: u.id, active: !u.is_active });
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">사용자 권한 (ERP 내부)</h1>
      <p className="text-xs text-slate-500">
        HIS는 ERP 접근(SSO)만 부여 — ERP 내부 역할은 여기서 별도 관리(ADMIN 전용). 변경 시 해당 사용자는 재인증됩니다.</p>
      {users.isError && <QueryError error={users.error} onRetry={() => users.refetch()} />}

      <Card title={`사용자 (${users.data?.items.length ?? 0}명)`}>
        <Table headers={["#", "이메일", "역할", "부서", "직원", "면허", "상태", "조치"]}
          rows={(users.data?.items ?? []).map((u) => [
            u.id,
            <span key="e">{u.email}{u.email_duplicate && <Badge tone="amber">대소문자 중복</Badge>}</span>,
            <select key="r" value={u.role} disabled={!canWrite}
              onChange={(e) => changeRole(u, e.target.value)}
              className="rounded border border-slate-300 px-2 py-1 text-sm">
              {ROLES.map((r) => <option key={r} value={r}>{ROLE_KO[r]}</option>)}
            </select>,
            u.department_id ?? "-", u.employee_id ?? "-",
            // FR-47 면허 유효 플래그 — 만료 시 진료 관련 접근이 차단되는데 화면에 근거가 없었다
            u.license_flag_active ? <Badge key="l" tone="gray">유효</Badge>
              : <Badge key="l" tone="amber">만료·미확인</Badge>,
            u.is_active ? <Badge key="a" tone="green">활성</Badge>
              : <Badge key="a" tone="red">비활성</Badge>,
            <span key="b" className="flex gap-1">
              {canWrite && <button onClick={() => toggleActive(u)}
                className="rounded bg-slate-500 px-2 py-0.5 text-xs text-white">
                {u.is_active ? "비활성화" : "활성화"}</button>}
              {canWrite && !!u.relink_candidate && (
                <button onClick={() => relinkUser(u)}
                  className="rounded bg-amber-600 px-2 py-0.5 text-xs text-white">재직 기록으로 재연결</button>)}
              {canWrite && u.email_duplicate && (
                <button onClick={() => retireDup(u)}
                  className="rounded bg-rose-600 px-2 py-0.5 text-xs text-white">중복 정리</button>)}
            </span>,
          ])} empty="사용자 없음" />
      </Card>
    </div>
  );
}
