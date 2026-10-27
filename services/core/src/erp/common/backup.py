"""백업/DR (design §6.5) — ERP DB 전체 pg_dump + 첨부 볼륨 tar → AES-256-GCM 암호화 → 세대 관리.

대상은 **ERP 전용 DB(`erp`) 전체**다 — 공유 PG 인스턴스라도 HIS는 다른 DB라 섞이지 않는다.
종전엔 스키마 9개를 고정 목록으로 넘겨, 이후 생긴 `itam`(0170)·`reporting`(0145)과 `public.alembic_version`·
확장(btree_gist)이 빠졌다 — 매일 '성공'했지만 복구하면 IT자산 데이터가 사라지고 alembic이 0001부터
다시 돌아 기동이 막히는 백업이었다(2026-10-09 전체 흐름 검토 H). 첨부(서명 완료본·증빙)는 볼륨에만
있어 DB 백업만으론 복구되지 않았다 → 같은 키로 암호화한 tar를 함께 남긴다.

스트리밍 (2026-10-09 전체 흐름 검토 2차 H): 종전엔 pg_dump 출력·첨부 tar·암호문을 **전부 메모리에**
들고 있었다 — worker-batch 메모리 한도(1G)를 넘으면 OOM이 스케줄러째 죽이고, 2^31-1바이트를 넘는
평문은 AESGCM이 OverflowError로 거부했다(DB가 자랄수록 언젠가 반드시 깨지는 백업). 이제
pg_dump→임시파일(0600·백업 디렉터리 안 비공개 임시 디렉터리), tar→임시파일, 그리고 **청크 단위 인증
암호화**(v2 프레임 포맷)로 출력 파일에 흘려 쓴다. 평문 임시파일은 finally에서 삭제한다.

v2 포맷:  MAGIC(b"ERPBK2\\n") ‖ file_id(16B 난수) ‖ 프레임*
  프레임 = [4B big-endian 암호문 길이][1B final 플래그][12B nonce][암호문(청크 ≤ 16MiB + 16B 태그)]
  AAD    = b"erp-backup-v2" ‖ file_id ‖ 청크 index(8B BE) ‖ final 플래그(1B)
  → 프레임 재배열(index 불일치)·다른 백업 프레임 접합(file_id 불일치)·꼬리 절단(final 프레임 부재)·
    final 이후 덧붙임을 모두 복호 실패로 검출한다.
v1(레거시) 포맷: nonce(12B) ‖ ciphertext (단일 블롭, AAD=b"erp-backup") — 복호만 지원.

복원: `python -m erp.common.backup decrypt <파일> <출력>` (v1·v2 자동 판별) → 런북 backup-restore.runbook.md.
복호 키는 백업 매체와 분리 보관(관리자). dump_fn 주입으로 테스트 가능.
"""
import glob
import inspect
import io
import logging
import os
import shutil
import struct
import subprocess
import tempfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("erp.common.backup")

_AAD = b"erp-backup"                 # v1(레거시 단일 블롭) AAD
_AAD_V2 = b"erp-backup-v2"
MAGIC_V2 = b"ERPBK2\n"
_FILE_ID_LEN = 16
_NONCE_LEN = 12
_TAG_LEN = 16
CHUNK_SIZE = 16 * 1024 * 1024        # 16MiB — 청크당 메모리 상한(AESGCM 2^31-1 한도와 무관)
_FRAME_HDR = struct.Struct(">IB")    # 암호문 길이(4B) + final 플래그(1B)
_TMP_PREFIX = ".tmp-backup-"


class BackupFormatError(ValueError):
    """백업 파일 손상·절단·재배열·키 불일치."""


def _private_file(path: str):
    """0600으로 새로 만든 쓰기 파일(이미 있으면 실패 — 평문이 다른 권한으로 남지 않게)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def pg_dump(database_url: str, out_dir: str | None = None) -> str | bytes:
    """ERP DB 전체 논리 백업(pg_dump) — 스키마 고정 목록 없음(신규 스키마 누락 방지).

    `--no-privileges`는 쓰지 않는다(2026-10-09 전체 흐름 검토 2차 H): 그 옵션은 GRANT/REVOKE를 빼서,
    복구본에서 원장 불변성 REVOKE(voucher_lines 0192·narcotics_ledger 0004·approval_ledger 0203·
    audit_log 0225)와 erp_bi GRANT가 사라졌다 — 복구하면 erp_app이 원장을 UPDATE/DELETE할 수 있게 된다.
    `--no-owner`는 유지(복구 대상 롤 이름이 달라도 적재되도록).

    out_dir 지정 시 stdout을 그 안의 0600 파일로 흘려 쓰고 경로를 반환(메모리 비점유). 미지정 시 bytes(호환).
    """
    sync_url = database_url.replace("+asyncpg", "")
    args = ["pg_dump", "--no-owner", sync_url]
    if out_dir is None:
        return subprocess.run(args, capture_output=True, check=True, timeout=3600).stdout
    out = os.path.join(out_dir, "erp.sql")
    with _private_file(out) as f:
        subprocess.run(args, stdout=f, stderr=subprocess.PIPE, check=True, timeout=3600)
    return out


def tar_dir(path: str | None, out_dir: str | None = None) -> str | bytes | None:
    """첨부 디렉터리 tar(gzip) — 없거나 비었으면 None. out_dir 지정 시 0600 파일로 쓰고 경로 반환."""
    import tarfile
    if not path or not os.path.isdir(path):
        return None
    if out_dir is None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(path, arcname="attachments")
        return buf.getvalue()
    out = os.path.join(out_dir, "attachments.tar.gz")
    with _private_file(out) as f, tarfile.open(fileobj=f, mode="w:gz") as tf:
        tf.add(path, arcname="attachments")
    return out


# ── v1 (레거시) ────────────────────────────────────────────────────────────────

def encrypt(data: bytes, key_hex: str) -> bytes:
    """[레거시 v1] AES-256-GCM — nonce(12B) ‖ ciphertext. 신규 백업은 encrypt_stream(v2)을 쓴다.
    작은 데이터·호환 테스트용으로만 남긴다(2^31-1바이트 초과 불가)."""
    nonce = os.urandom(_NONCE_LEN)
    return nonce + AESGCM(bytes.fromhex(key_hex)).encrypt(nonce, data, _AAD)


# ── v2 (청크 프레임) ────────────────────────────────────────────────────────────

def _aad_v2(file_id: bytes, index: int, final: bool) -> bytes:
    return _AAD_V2 + file_id + struct.pack(">QB", index, 1 if final else 0)


def encrypt_stream(src, dst, key_hex: str, *, chunk_size: int = CHUNK_SIZE) -> int:
    """src(읽기 파일객체)를 v2 프레임으로 dst(쓰기 파일객체)에 암호화. 반환: 평문 바이트 수.

    한 번에 청크 2개(현재+선읽기)만 메모리에 둔다 — 선읽기로 마지막 청크에 final 플래그를 붙인다."""
    if chunk_size <= 0:
        raise ValueError("chunk_size는 양수")
    aes = AESGCM(bytes.fromhex(key_hex))
    file_id = os.urandom(_FILE_ID_LEN)
    dst.write(MAGIC_V2 + file_id)
    total, index = 0, 0
    cur = src.read(chunk_size)
    while True:
        nxt = src.read(chunk_size) if cur else b""
        final = not nxt
        nonce = os.urandom(_NONCE_LEN)
        ct = aes.encrypt(nonce, cur, _aad_v2(file_id, index, final))
        dst.write(_FRAME_HDR.pack(len(ct), 1 if final else 0) + nonce + ct)
        total += len(cur)
        if final:
            return total
        cur, index = nxt, index + 1


def _read_exact(src, n: int) -> bytes:
    buf = src.read(n)
    if len(buf) != n:
        raise BackupFormatError("백업 파일이 잘렸습니다(프레임 중간 EOF)")
    return buf


def decrypt_stream(src, dst, key_hex: str, *, max_frame: int = CHUNK_SIZE + _TAG_LEN) -> int:
    """v2 프레임 복호(src는 MAGIC 직후 위치가 아니라 파일 처음부터). 반환: 평문 바이트 수.
    final 프레임 부재(절단)·재배열·접합·final 이후 덧붙임 → BackupFormatError."""
    from cryptography.exceptions import InvalidTag
    if src.read(len(MAGIC_V2)) != MAGIC_V2:
        raise BackupFormatError("v2 백업 포맷이 아닙니다")
    aes = AESGCM(bytes.fromhex(key_hex))
    file_id = _read_exact(src, _FILE_ID_LEN)
    total, index = 0, 0
    while True:
        hdr = src.read(_FRAME_HDR.size)
        if not hdr:
            raise BackupFormatError("백업 파일이 잘렸습니다(마지막 프레임 없음)")
        if len(hdr) != _FRAME_HDR.size:
            raise BackupFormatError("백업 파일이 잘렸습니다(프레임 헤더 EOF)")
        ct_len, flag = _FRAME_HDR.unpack(hdr)
        if flag not in (0, 1) or ct_len < _TAG_LEN or ct_len > max_frame:
            raise BackupFormatError(f"프레임 {index} 헤더 손상")
        nonce = _read_exact(src, _NONCE_LEN)
        ct = _read_exact(src, ct_len)
        try:
            pt = aes.decrypt(nonce, ct, _aad_v2(file_id, index, flag == 1))
        except InvalidTag as exc:
            raise BackupFormatError(
                f"프레임 {index} 인증 실패 — 키 불일치·손상·재배열") from exc
        dst.write(pt)
        total += len(pt)
        if flag == 1:
            if src.read(1):
                raise BackupFormatError("마지막 프레임 뒤에 덧붙은 데이터")
            return total
        index += 1


def decrypt(blob: bytes, key_hex: str, *, max_frame: int = CHUNK_SIZE + _TAG_LEN) -> bytes:
    """메모리 복호 — v2(MAGIC)·v1(nonce‖ct) 자동 판별. 대용량 복원은 decrypt_file을 쓴다."""
    if blob.startswith(MAGIC_V2):
        out = io.BytesIO()
        decrypt_stream(io.BytesIO(blob), out, key_hex, max_frame=max_frame)
        return out.getvalue()
    return AESGCM(bytes.fromhex(key_hex)).decrypt(blob[:_NONCE_LEN], blob[_NONCE_LEN:], _AAD)


def decrypt_file(src_path: str, dst_path: str, key_hex: str) -> int:
    """파일 복호 — v2는 스트리밍, v1(레거시)은 통째 로드. 출력은 0600 신규 파일. 실패 시 출력 삭제."""
    created = False      # 이 호출이 만든 출력만 지운다 — 기존 파일을 지우던 결함(3차 검토 H)
    try:
        with open(src_path, "rb") as src, _private_file(dst_path) as dst:
            created = True
            if src.read(len(MAGIC_V2)) == MAGIC_V2:
                src.seek(0)
                return decrypt_stream(src, dst, key_hex)
            src.seek(0)
            out = decrypt(src.read(), key_hex)
            dst.write(out)
            return len(out)
    except BaseException:
        if created and os.path.exists(dst_path):
            os.unlink(dst_path)
        raise


def _encrypt_to(path: str, payload, key_hex: str, chunk_size: int) -> None:
    """payload(bytes 또는 평문 파일 경로)를 v2로 path에 원자적 기록(.part → rename, 0600)."""
    part = path + ".part"
    if os.path.exists(part):
        os.unlink(part)
    try:
        with _private_file(part) as dst:
            if isinstance(payload, (bytes, bytearray, memoryview)):
                encrypt_stream(io.BytesIO(bytes(payload)), dst, key_hex, chunk_size=chunk_size)
            else:
                with open(payload, "rb") as src:
                    encrypt_stream(src, dst, key_hex, chunk_size=chunk_size)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(part, path)
    except BaseException:
        if os.path.exists(part):
            os.unlink(part)
        raise


def _call_source(fn, workdir: str):
    """dump_fn/attachments_fn 호출 — 위치 인자 1개를 받으면 비공개 임시 디렉터리를 넘긴다
    (그 안에 평문을 쓰고 경로 반환 → run_backup이 삭제). 무인자 함수는 bytes 반환(호환)."""
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)]
    except (TypeError, ValueError):
        params = []
    return fn(workdir) if params else fn()


def attachments_backup_path(backup_dir: str, today_iso: str) -> str:
    return os.path.join(backup_dir, f"erp-attachments-{today_iso}.tar.gz.enc")


def _purge_stale_temp(backup_dir: str) -> None:
    """이전 회차가 죽으며(OOM 등) 남긴 평문 임시 디렉터리·.part 정리 — 평문이 디스크에 방치되지 않게."""
    for d in glob.glob(os.path.join(backup_dir, _TMP_PREFIX + "*")):
        shutil.rmtree(d, ignore_errors=True)
    for p in glob.glob(os.path.join(backup_dir, "erp-*.enc.part")):
        try:
            os.unlink(p)
        except OSError:
            pass


def run_backup(*, backup_dir: str, enc_key_hex: str, today_iso: str,
               keep_daily: int = 7, dump_fn=None, attachments_fn=None,
               chunk_size: int = CHUNK_SIZE) -> str:
    """덤프 → 스트리밍 암호화(v2) → backup_dir에 저장 → 일 세대 정리. 반환: DB 백업 경로.

    dump_fn/attachments_fn: `fn(workdir) -> 평문 파일 경로`(운영·스트리밍) 또는 `fn() -> bytes`(테스트·호환).
    workdir는 backup_dir 안의 0700 비공개 임시 디렉터리 — 평문은 그 안에만 생기고 finally에서 통째 삭제된다.
    첨부 백업본 경로는 attachments_backup_path(backup_dir, today_iso).
    """
    if dump_fn is None:
        raise ValueError("dump_fn이 필요합니다 (운영: pg_dump)")
    os.makedirs(backup_dir, exist_ok=True)
    _purge_stale_temp(backup_dir)
    path = os.path.join(backup_dir, f"erp-{today_iso}.sql.enc")
    workdir = tempfile.mkdtemp(prefix=_TMP_PREFIX, dir=backup_dir)   # mkdtemp = 0700
    try:
        data = _call_source(dump_fn, workdir)
        if data is None:
            raise ValueError("dump_fn이 결과를 반환하지 않았습니다")
        _encrypt_to(path, data, enc_key_hex, chunk_size)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            os.unlink(data)                                  # 평문 즉시 삭제(디스크 점유 최소화)
        att = _call_source(attachments_fn, workdir) if attachments_fn else None
        if att:
            _encrypt_to(attachments_backup_path(backup_dir, today_iso), att, enc_key_hex, chunk_size)
            if not isinstance(att, (bytes, bytearray, memoryview)):
                os.unlink(att)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)            # 남은 평문 임시파일 일괄 삭제
    # 세대 관리 — 최근 keep_daily개만 보존(DB·첨부 각각)
    removed = []
    for pattern in ("erp-[0-9]*.sql.enc", "erp-attachments-*.tar.gz.enc"):
        files = sorted(glob.glob(os.path.join(backup_dir, pattern)))
        for old in files[:-keep_daily] if keep_daily > 0 else []:
            os.remove(old)
            removed.append(os.path.basename(old))
    logger.info("백업 완료", extra={"ctx": {
        "path": os.path.basename(path), "bytes": os.path.getsize(path),
        "purged": removed}})
    return path


def push_offsite(local_path: str, *, dest: str, ssh_port: int = 22,
                 ssh_key: str | None = None, timeout: int = 600) -> str:
    """암호화 백업본을 오프사이트로 전송 (재해복구 P2-1·rsync over SSH·시스템 바이너리·Python 의존 0).

    백업본은 이미 AES-GCM 암호문이라 전송경로 무관하나 SSH가 전송 무결성·인증 제공. 실패 시 예외
    (호출부 job_backup이 ADMIN 알림). BatchMode=yes로 무비번(키 전용)·accept-new로 최초 호스트키 TOFU.
    dest 예: "backup@10.0.0.9:/data/erp-offsite/". ssh_key 미지정 시 기본 키(에이전트/~/.ssh) 사용.
    """
    ssh = f"ssh -p {ssh_port} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
    if ssh_key:
        ssh += f" -i {ssh_key}"
    r = subprocess.run(["rsync", "-az", "-e", ssh, local_path, dest],
                       capture_output=True, text=True, timeout=timeout, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"오프사이트 rsync 실패(rc={r.returncode}): {r.stderr[:300]}")
    logger.info("오프사이트 백업 전송", extra={"ctx": {"dest": dest,
                "file": os.path.basename(local_path)}})
    return dest


def _main(argv: list[str]) -> int:
    """복원용 복호화 CLI — `python -m erp.common.backup decrypt <입력.enc> <출력>` (v1·v2 자동 판별).

    키: BACKUP_ENC_KEY(없으면 DATA_ENC_KEY — job_backup과 같은 규칙). 출력은 0600 신규 파일(기존 파일 덮어쓰기 거부)."""
    if len(argv) != 3 or argv[0] != "decrypt":
        print("사용: python -m erp.common.backup decrypt <입력.enc> <출력>")
        return 2
    from erp.config import get_settings
    s = get_settings()
    key = s.backup_enc_key or s.data_enc_key
    n = decrypt_file(argv[1], argv[2], key)
    print(f"복호화 완료: {argv[2]} ({n} bytes) — 복원 후 shred -u 로 평문 삭제")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
