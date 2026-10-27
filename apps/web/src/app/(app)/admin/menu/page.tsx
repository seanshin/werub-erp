"use client";
/** 메뉴 권한 (역할별 노출 설정) — DB 오버라이드. ADMIN 저장. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Button, Card } from "@/components/ui";
import { useToast } from "@/components/toast";

interface MItem { menu_key: string; label: string; group: string | null;
  // 기관 단위 비활성(역할 무관 전역) — 그룹웨어 등 외부 시스템과 중복되는 화면 숨김
  org_disabled: boolean; undisableable: boolean;
  visible: Record<string, boolean> }
// 그룹웨어 전자결재가 별도로 있으면 중복 입구가 되는 ERP 셀프서비스 화면(사용자 지목)
const GROUPWARE_DUP = ["/hr/attendance", "/hr/leaves", "/hr/welfare", "/finance-expenses"];
const ROLE_KO: Record<string, string> = {
  DIRECTOR: "원장", ADMIN: "관리자", MANAGER: "부서장", STAFF: "직원",
  IT_MANAGER: "IT관리자", FINANCE_MANAGER: "회계관리자", LOGISTICS_MANAGER: "물류관리자" };

export default function MenuPermPage() {
  const qc = useQueryClient();
  const toast = useToast();
  // 변경분만 추적(effect로 전체 복사 회피) — 표시값 = edits[key][role] ?? 서버값
  const [edits, setEdits] = useState<Record<string, Record<string, boolean>>>({});
  // 기관 비활성도 변경분만 추적 — 표시값 = orgEdits[key] ?? 서버값
  const [orgEdits, setOrgEdits] = useState<Record<string, boolean>>({});
  const perm = useQuery({ queryKey: ["menu-perms"],
    queryFn: () => api<{ roles: string[]; items: MItem[] }>("/admin/menu-permissions") });

  const roles = perm.data?.roles ?? [];
  const items = perm.data?.items ?? [];
  const checked = (it: MItem, r: string) => edits[it.menu_key]?.[r] ?? it.visible[r];
  const toggle = (it: MItem, r: string) =>
    setEdits((p) => ({ ...p, [it.menu_key]: { ...p[it.menu_key], [r]: !checked(it, r) } }));
  const disabled = (it: MItem) => orgEdits[it.menu_key] ?? it.org_disabled;
  const toggleOrg = (it: MItem) =>
    setOrgEdits((p) => ({ ...p, [it.menu_key]: !disabled(it) }));
  const applyPreset = () =>
    setOrgEdits((p) => ({ ...p, ...Object.fromEntries(GROUPWARE_DUP.map((k) => [k, true])) }));

  const save = useMutation({
    mutationFn: async () => {
      if (Object.keys(orgEdits).length > 0) {
        // 전량 치환 EP — 서버 현재값에 변경분을 덮어 최종 목록을 만들어 보낸다
        const menu_keys = items.filter((it) => disabled(it)).map((it) => it.menu_key);
        await api("/admin/menu-org-disabled", { method: "PUT",
          body: JSON.stringify({ menu_keys }) });
      }
      const overrides: { menu_key: string; role: string; visible: boolean }[] = [];
      Object.entries(edits).forEach(([k, rv]) =>
        Object.entries(rv).forEach(([r, v]) => overrides.push({ menu_key: k, role: r, visible: v })));
      if (overrides.length === 0) return;
      return api("/admin/menu-permissions", { method: "PUT",
        body: JSON.stringify({ overrides }) });
    },
    onSuccess: () => { toast.ok("저장 완료 — 사용자는 재로그인/새로고침 시 반영");
      setEdits({}); setOrgEdits({});
      qc.invalidateQueries({ queryKey: ["menu-perms"] });
      qc.invalidateQueries({ queryKey: ["menu"] }); },
    onError: (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패"),
  });
  const dirty = Object.keys(edits).length > 0 || Object.keys(orgEdits).length > 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">메뉴 권한 (역할별 노출)</h1>
        <Button isLoading={save.isPending} disabled={!dirty} onClick={() => save.mutate()}>저장</Button>
      </div>
      <p className="text-xs text-slate-500">
        역할별 사이드바 메뉴 노출 설정. 권한(API 접근)은 서버 정책이 강제 — 메뉴 노출과 별개.</p>

      <Card title="기관 단위 메뉴 사용 안 함 (역할 무관)"
        action={<Button variant="secondary" onClick={applyPreset}>그룹웨어 전자결재 중복 화면 일괄 선택</Button>}>
        <p className="text-xs text-slate-500">
          그룹웨어 전자결재처럼 <b>같은 일을 하는 시스템이 따로 있는 경우</b>, 아래 표의
          &lsquo;사용 안 함&rsquo;을 체크하면 <b>모든 역할</b>에서 해당 메뉴가 사라집니다.
          역할별 체크박스를 7개 다 끄지 않아도 되고, 하나를 놓쳐 노출되는 사고가 없습니다.
        </p>
        <p className="mt-1 text-xs text-amber-700">
          메뉴만 숨깁니다 — API·데이터는 그대로 두어 기존 기록 조회와 HIS 연동 수신은 계속됩니다.
          휴가 신청은 그룹웨어로 상신이 연동돼 있어, 숨기면 직원은 그룹웨어에서 직접 상신하게 됩니다.
        </p>
      </Card>
      {perm.isError && <p className="text-sm text-red-600">메뉴 권한 조회 권한이 없습니다 (DIRECTOR/ADMIN)</p>}

      <Card title={`메뉴 × 역할 (${items.length}개 메뉴)`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 text-left text-[11px] uppercase text-slate-500">
                <th className="px-3 py-2">메뉴</th>
                <th className="px-3 py-2 text-center">사용 안 함</th>
                {roles.map((r) => <th key={r} className="px-3 py-2 text-center">{ROLE_KO[r] ?? r}</th>)}
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.menu_key}
                  className={`border-b border-slate-100 ${disabled(it) ? "bg-slate-50 text-slate-400" : ""}`}>
                  <td className="px-3 py-1.5">
                    <span className="text-[11px] text-slate-400">{it.group ?? "공통"} · </span>
                    {it.label}
                  </td>
                  <td className="px-3 py-1.5 text-center">
                    {it.undisableable
                      ? <span className="text-[11px] text-slate-400" title="이 메뉴를 숨기면 설정을 되돌릴 수 없어 제외">—</span>
                      : <input type="checkbox" checked={disabled(it)} onChange={() => toggleOrg(it)} />}
                  </td>
                  {roles.map((r) => (
                    <td key={r} className="px-3 py-1.5 text-center">
                      <input type="checkbox" checked={checked(it, r)}
                        disabled={disabled(it)} onChange={() => toggle(it, r)} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
