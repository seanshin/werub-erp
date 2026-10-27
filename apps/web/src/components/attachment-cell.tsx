"use client";
/** 증빙 첨부 셀 (재사용) — entity_type/id에 파일 업로드·다운로드(증빙 보관).
 *
 * gallery=true 시 이미지 첨부를 썸네일로 표시(자산 사진 등). 비이미지는 📎 유지.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, apiDownload, apiUpload, apiUrl } from "@/lib/api";

interface Att { id: number; filename: string; content_type?: string | null }

export function AttachmentCell({ entityType, entityId, gallery }: {
  entityType: "tax_invoice" | "voucher" | "po_receipt" | "contract" | "employee"
    | "it_asset" | "itam_contract" | "software_service" | "itam_maintenance"
    | "itam_offboarding" | "org" | "other";
  entityId: number;
  gallery?: boolean;
}) {
  const qc = useQueryClient();
  const key = ["att", entityType, entityId];
  const atts = useQuery({
    queryKey: key,
    queryFn: () => api<{ items: Att[] }>(
      `/attachments?entity_type=${entityType}&entity_id=${entityId}`),
  });
  const up = useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return apiUpload(`/attachments?entity_type=${entityType}&entity_id=${entityId}`, fd);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: key }),
  });
  const items = atts.data?.items ?? [];
  const isImg = (a: Att) => (a.content_type ?? "").startsWith("image/")
    || /\.(png|jpe?g|webp)$/i.test(a.filename);
  const pickFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]; if (f) up.mutate(f); e.target.value = "";
  };

  if (gallery) {
    return (
      <div className="flex flex-wrap items-center gap-2">
        {items.map((a) => isImg(a) ? (
          <a key={a.id} href={apiUrl(`/attachments/${a.id}/download`)} target="_blank" rel="noreferrer"
            title={a.filename}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img src={apiUrl(`/attachments/${a.id}/download`)} alt={a.filename}
              className="h-16 w-16 rounded-md border border-slate-200 object-cover" />
          </a>
        ) : (
          <button key={a.id} title={a.filename}
            onClick={() => apiDownload(`/attachments/${a.id}/download`, a.filename)}
            className="flex h-16 w-16 items-center justify-center rounded-md border border-slate-200 text-xs text-blue-600">📎</button>
        ))}
        <label className="flex h-16 w-16 cursor-pointer items-center justify-center rounded-md border border-dashed border-slate-300 text-lg text-slate-400 hover:bg-slate-50">
          {up.isPending ? "…" : "＋"}
          <input type="file" className="hidden" accept=".pdf,.png,.jpg,.jpeg,.webp,.xlsx,.csv"
            onChange={pickFile} />
        </label>
      </div>
    );
  }

  return (
    <span className="flex items-center gap-1">
      {items.map((a) => (
        <button key={a.id} title={a.filename}
          onClick={() => apiDownload(`/attachments/${a.id}/download`, a.filename)}
          className="text-xs text-blue-600 underline">📎</button>
      ))}
      <label className="cursor-pointer rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600 hover:bg-slate-200">
        {up.isPending ? "…" : "+"}
        <input type="file" className="hidden"
          accept=".pdf,.png,.jpg,.jpeg,.webp,.xlsx,.csv" onChange={pickFile} />
      </label>
    </span>
  );
}
