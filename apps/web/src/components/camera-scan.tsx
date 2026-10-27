"use client";
/** 카메라 바코드 스캔 — 브라우저 내장 BarcodeDetector(안드로이드 Chrome 등) 무의존. 미지원 시 버튼 숨김.
 *  모바일 워크스테이션에서 하드웨어 스캐너 없이 폰/태블릿 카메라로 바코드 스캔. */
import { useEffect, useRef, useState } from "react";

interface BD { detect: (src: CanvasImageSource) => Promise<{ rawValue: string }[]> }
declare global {
  interface Window { BarcodeDetector?: new (opts?: { formats?: string[] }) => BD }
}

const FORMATS = ["ean_13", "ean_8", "code_128", "code_39", "upc_a", "upc_e",
  "itf", "qr_code", "data_matrix"];

export function CameraScanButton({ onScan }: { onScan: (code: string) => void }) {
  const [open, setOpen] = useState(false);
  const [supported, setSupported] = useState(false);
  useEffect(() => {
    // 클라이언트 전용 기능감지(BarcodeDetector) — SSR 후 마운트 시점에 판정
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSupported("BarcodeDetector" in window);
  }, []);
  if (!supported) return null;
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}
        className="shrink-0 rounded-xl border-2 border-blue-200 bg-white px-4 py-3 text-sm font-semibold text-blue-700 active:bg-blue-50">
        📷 카메라
      </button>
      {open && <CameraOverlay onClose={() => setOpen(false)}
        onScan={(c) => { onScan(c); setOpen(false); }} />}
    </>
  );
}

function CameraOverlay({ onScan, onClose }: { onScan: (c: string) => void; onClose: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let stream: MediaStream | null = null;
    let raf = 0;
    let stopped = false;
    const detector = new window.BarcodeDetector!({ formats: FORMATS });
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
        const v = videoRef.current;
        if (!v) return;
        v.srcObject = stream;
        await v.play();
        const tick = async () => {
          if (stopped || !videoRef.current) return;
          try {
            const codes = await detector.detect(videoRef.current);
            if (codes[0]?.rawValue) { onScan(codes[0].rawValue); return; }
          } catch { /* 일시적 실패 무시 */ }
          raf = requestAnimationFrame(tick);
        };
        raf = requestAnimationFrame(tick);
      } catch { setErr("카메라 접근 실패 — 브라우저 권한을 확인하세요"); }
    })();
    return () => {
      stopped = true;
      cancelAnimationFrame(raf);
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [onScan]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/90 p-4" onClick={onClose}>
      <div className="w-full max-w-md" onClick={(e) => e.stopPropagation()}>
        <div className="relative overflow-hidden rounded-2xl">
          <video ref={videoRef} playsInline muted className="w-full bg-black" />
          <div className="pointer-events-none absolute inset-x-10 top-1/2 h-0.5 -translate-y-1/2 bg-red-500/80" />
        </div>
        <p className="mt-3 text-center text-sm text-white">
          바코드를 화면 중앙 선에 맞추세요{err && ` · ${err}`}</p>
        <button onClick={onClose}
          className="mt-3 w-full rounded-xl bg-white/90 py-3 text-base font-semibold text-slate-800">
          닫기
        </button>
      </div>
    </div>
  );
}
