"use client";
/** QR 코드 — 자산 태그 라벨(생성·표시·인쇄). qrcode(무-CDN·번들) 사용. */
import { useEffect, useState } from "react";
import QRCode from "qrcode";

export function QrImage({ value, size = 140 }: { value: string; size?: number }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let alive = true;
    QRCode.toDataURL(value, { margin: 1, width: size * 2, errorCorrectionLevel: "M" })
      .then((u) => { if (alive) setUrl(u); })
      .catch(() => { if (alive) setUrl(""); });
    return () => { alive = false; };
  }, [value, size]);
  if (!url) return <div style={{ width: size, height: size }} className="animate-pulse rounded bg-slate-100" />;
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={url} alt={`QR ${value}`} width={size} height={size} className="rounded bg-white" />;
}

function esc(s: string): string {
  return s.replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch] as string));
}

/** 자산 태그 QR 라벨 인쇄 — QR + 자산명·태그·부서 (별도 창 print). 텍스트는 HTML 이스케이프. */
export async function printQrLabel(tag: string, lines: string[]): Promise<void> {
  const dataUrl = await QRCode.toDataURL(tag, { margin: 1, width: 280, errorCorrectionLevel: "M" });
  const w = window.open("", "_blank", "width=380,height=460");
  if (!w) return;
  const body = `<div style="text-align:center;font-family:sans-serif;padding:12px">` +
    `<img src="${dataUrl}" style="width:200px;height:200px"/>` +
    `<div style="font-size:16px;font-weight:700;margin-top:6px">${esc(tag)}</div>` +
    lines.map((l) => `<div style="font-size:13px;color:#334">${esc(l)}</div>`).join("") +
    `</div>`;
  w.document.write(`<html><head><title>${esc(tag)}</title></head><body onload="window.print()">${body}</body></html>`);
  w.document.close();
}
