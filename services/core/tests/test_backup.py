"""백업/DR (design §6.5) — AES-GCM 암호화·세대 관리."""
import glob
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import pytest

from erp.common.backup import (
    _AAD,
    MAGIC_V2,
    BackupFormatError,
    decrypt,
    encrypt,
    run_backup,
)

KEY = "00" * 32  # 32바이트 hex


def test_encrypt_roundtrip():
    blob = encrypt(b"hello erp", KEY)
    nonce, ct = blob[:12], blob[12:]
    assert AESGCM(bytes.fromhex(KEY)).decrypt(nonce, ct, _AAD) == b"hello erp"
    assert len(nonce) == 12 and ct != b"hello erp"  # 암호화됨


def test_run_backup_creates_encrypted_file(tmp_path):
    path = run_backup(backup_dir=str(tmp_path), enc_key_hex=KEY, today_iso="2026-06-08",
                      keep_daily=7, dump_fn=lambda: b"FAKE DUMP DATA")
    assert os.path.exists(path) and path.endswith("erp-2026-06-08.sql.enc")
    assert oct(os.stat(path).st_mode)[-3:] == "600"  # 권한 600
    blob = open(path, "rb").read()
    assert blob.startswith(MAGIC_V2)                         # 신규 백업은 v2 프레임 포맷
    assert decrypt(blob, KEY) == b"FAKE DUMP DATA"


def test_run_backup_generation_cleanup(tmp_path):
    for day in range(1, 10):  # 9세대 생성, keep=3
        run_backup(backup_dir=str(tmp_path), enc_key_hex=KEY,
                   today_iso=f"2026-06-{day:02d}", keep_daily=3, dump_fn=lambda: b"x")
    files = sorted(glob.glob(os.path.join(str(tmp_path), "erp-*.sql.enc")))
    assert len(files) == 3                                   # 최근 3개만
    assert files[-1].endswith("erp-2026-06-09.sql.enc")      # 최신 보존
    assert not os.path.exists(os.path.join(str(tmp_path), "erp-2026-06-01.sql.enc"))  # 만료 파기


def test_push_offsite_builds_rsync_over_ssh(monkeypatch, tmp_path):
    """오프사이트 전송 — rsync -az -e ssh 명령 구성·성공/실패 전파(P2-1)."""
    import subprocess as _sp

    import pytest

    from erp.common import backup as _b
    f = tmp_path / "erp-2026-07-19.sql.enc"
    f.write_bytes(b"cipher")
    calls = {}

    class _R:
        def __init__(self, rc, err=""):
            self.returncode, self.stderr = rc, err

    def _fake_run(cmd, **kw):
        calls["cmd"] = cmd
        return _R(0)
    monkeypatch.setattr(_sp, "run", _fake_run)
    dest = _b.push_offsite(str(f), dest="backup@10.0.0.9:/data/off/", ssh_port=2222,
                           ssh_key="/run/secrets/bkey")
    assert dest == "backup@10.0.0.9:/data/off/"
    assert calls["cmd"][0] == "rsync" and "-az" in calls["cmd"] and str(f) in calls["cmd"]
    ssh_arg = calls["cmd"][calls["cmd"].index("-e") + 1]
    assert "-p 2222" in ssh_arg and "BatchMode=yes" in ssh_arg and "-i /run/secrets/bkey" in ssh_arg

    # rsync 실패 → 예외(job_backup이 ADMIN 경보)
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: _R(23, "permission denied"))
    with pytest.raises(RuntimeError, match="rsync 실패"):
        _b.push_offsite(str(f), dest="x@h:/p/")


def test_dump_covers_whole_database_not_fixed_schema_list(monkeypatch):
    """전체 흐름 검토 H — 고정 스키마 목록이 itam·reporting·alembic_version을 빠뜨렸다. DB 전체를 덤프한다."""
    from erp.common import backup
    seen = {}

    class _R:
        stdout = b"dump"

    def fake_run(args, **kw):
        seen["args"] = args
        return _R()
    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    backup.pg_dump("postgresql+asyncpg://u:p@h:5432/erp")
    assert not any(a.startswith("--schema") for a in seen["args"])
    # 2026-10-09 전체 흐름 검토 2차 H — --no-privileges는 원장 불변성 REVOKE·erp_bi GRANT를 복구본에서 지운다
    assert "--no-privileges" not in seen["args"] and "--no-owner" in seen["args"]
    assert seen["args"][-1] == "postgresql://u:p@h:5432/erp"


def test_model_schemas_all_inside_backup_scope():
    """신규 스키마가 생겨도 백업에서 빠지지 않는다 — 스키마 필터 자체가 없음을 고정."""
    import inspect

    from erp.common import backup
    assert "--schema" not in inspect.getsource(backup.pg_dump)


def test_attachments_tar_backed_up_and_restorable(tmp_path):
    import io
    import tarfile

    from erp.common.backup import decrypt, run_backup, tar_dir
    att = tmp_path / "att" / "ab"
    att.mkdir(parents=True)
    (att / "f1").write_bytes(b"%PDF-1.4")
    key = "1" * 64
    run_backup(backup_dir=str(tmp_path / "bk"), enc_key_hex=key, today_iso="2026-10-09",
               dump_fn=lambda: b"SQL", attachments_fn=lambda: tar_dir(str(tmp_path / "att")))
    enc = (tmp_path / "bk" / "erp-attachments-2026-10-09.tar.gz.enc").read_bytes()
    with tarfile.open(fileobj=io.BytesIO(decrypt(enc, key))) as tf:
        assert "attachments/ab/f1" in tf.getnames()


def test_decrypt_cli_roundtrip(tmp_path, monkeypatch):
    from erp.common import backup
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "backup_enc_key", "2" * 64, raising=False)
    src = tmp_path / "x.enc"
    src.write_bytes(backup.encrypt(b"hello", "2" * 64))              # v1(레거시) 파일
    assert backup._main(["decrypt", str(src), str(tmp_path / "x.sql")]) == 0
    assert (tmp_path / "x.sql").read_bytes() == b"hello"
    assert oct(os.stat(tmp_path / "x.sql").st_mode)[-3:] == "600"
    # v2(신규) 파일도 같은 CLI로 — 실제 run_backup 산출물
    p = run_backup(backup_dir=str(tmp_path / "bk"), enc_key_hex="2" * 64, today_iso="2026-10-09",
                   dump_fn=lambda: b"v2-body" * 100, chunk_size=64)
    assert backup._main(["decrypt", p, str(tmp_path / "y.sql")]) == 0
    assert (tmp_path / "y.sql").read_bytes() == b"v2-body" * 100



# ── 2026-10-09 전체 흐름 검토 2차 H — 스트리밍·청크 인증 암호화(v2) ─────────────────

def _frames(blob: bytes):
    """v2 블롭 → (헤더(MAGIC+file_id), [프레임 바이트...])."""
    import struct
    head_len = len(MAGIC_V2) + 16
    head, rest, frames = blob[:head_len], blob[head_len:], []
    while rest:
        ct_len, _flag = struct.unpack(">IB", rest[:5])
        n = 5 + 12 + ct_len
        frames.append(rest[:n])
        rest = rest[n:]
    return head, frames


def _multi_chunk_backup(tmp_path, data: bytes, chunk: int = 1000) -> bytes:
    p = run_backup(backup_dir=str(tmp_path), enc_key_hex=KEY, today_iso="2026-10-09",
                   dump_fn=lambda: data, chunk_size=chunk)
    return open(p, "rb").read()


def test_v2_multi_chunk_roundtrip(tmp_path):
    data = os.urandom(4500)                                  # 1000B 청크 → 5프레임
    blob = _multi_chunk_backup(tmp_path, data)
    _head, frames = _frames(blob)
    assert len(frames) == 5
    assert decrypt(blob, KEY) == data


def test_v2_exact_multiple_and_empty(tmp_path):
    from erp.common.backup import decrypt_stream, encrypt_stream
    import io
    for data in (b"", b"a" * 3000):                          # 빈 입력·청크 배수 경계
        out = io.BytesIO()
        assert encrypt_stream(io.BytesIO(data), out, KEY, chunk_size=1000) == len(data)
        back = io.BytesIO()
        out.seek(0)
        assert decrypt_stream(out, back, KEY) == len(data) and back.getvalue() == data


def test_v2_truncation_detected(tmp_path):
    """마지막 프레임을 떼어내면(꼬리 절단) — 앞 프레임들은 모두 유효해도 복호 실패."""
    blob = _multi_chunk_backup(tmp_path, os.urandom(4500))
    head, frames = _frames(blob)
    with pytest.raises(BackupFormatError, match="잘렸"):
        decrypt(head + b"".join(frames[:-1]), KEY)
    with pytest.raises(BackupFormatError):
        decrypt(blob[:-7], KEY)                               # 프레임 중간 절단


def test_v2_reorder_and_splice_detected(tmp_path):
    data = os.urandom(4500)
    blob = _multi_chunk_backup(tmp_path / "a", data)
    head, frames = _frames(blob)
    swapped = head + frames[1] + frames[0] + b"".join(frames[2:])
    with pytest.raises(BackupFormatError, match="인증 실패"):
        decrypt(swapped, KEY)
    # 다른 백업(같은 키)의 프레임 접합 — file_id가 달라 실패
    other_head, other_frames = _frames(_multi_chunk_backup(tmp_path / "b", data))
    with pytest.raises(BackupFormatError):
        decrypt(head + other_frames[0] + b"".join(frames[1:]), KEY)
    with pytest.raises(BackupFormatError, match="덧붙"):
        decrypt(blob + b"x", KEY)


def test_v1_legacy_blob_still_decrypts(tmp_path):
    from erp.common.backup import decrypt_file
    legacy = encrypt(b"OLD FORMAT DUMP", KEY)
    assert decrypt(legacy, KEY) == b"OLD FORMAT DUMP"
    src = tmp_path / "old.sql.enc"
    src.write_bytes(legacy)
    assert decrypt_file(str(src), str(tmp_path / "old.sql"), KEY) == len(b"OLD FORMAT DUMP")
    assert (tmp_path / "old.sql").read_bytes() == b"OLD FORMAT DUMP"


def test_streaming_sources_are_files_and_plaintext_removed(tmp_path):
    """dump_fn(workdir)→평문 파일 경로 — 0700 임시 디렉터리 안에서 생성되고 백업 후 평문이 남지 않는다."""
    seen = {}

    def dump(workdir):
        seen["mode"] = oct(os.stat(workdir).st_mode)[-3:]
        p = os.path.join(workdir, "erp.sql")
        with open(p, "wb") as f:
            f.write(b"S" * 2500)
        seen["p"] = p
        return p
    att_dir = tmp_path / "att"
    att_dir.mkdir()
    (att_dir / "f").write_bytes(b"PDF")
    from erp.common.backup import attachments_backup_path, tar_dir
    bk = tmp_path / "bk"
    path = run_backup(backup_dir=str(bk), enc_key_hex=KEY, today_iso="2026-10-09",
                      dump_fn=dump, attachments_fn=lambda w: tar_dir(str(att_dir), out_dir=w),
                      chunk_size=1000)
    assert seen["mode"] == "700"
    assert not os.path.exists(seen["p"])
    assert sorted(os.listdir(bk)) == ["erp-2026-10-09.sql.enc", "erp-attachments-2026-10-09.tar.gz.enc"]
    assert decrypt(open(path, "rb").read(), KEY) == b"S" * 2500
    assert os.path.exists(attachments_backup_path(str(bk), "2026-10-09"))


def test_failed_dump_leaves_no_plaintext_or_partial(tmp_path):
    def dump(workdir):
        with open(os.path.join(workdir, "erp.sql"), "wb") as f:
            f.write(b"secret")
        raise RuntimeError("pg_dump died")
    with pytest.raises(RuntimeError):
        run_backup(backup_dir=str(tmp_path), enc_key_hex=KEY, today_iso="2026-10-09", dump_fn=dump)
    assert os.listdir(tmp_path) == []


def test_pg_dump_streams_to_file(monkeypatch, tmp_path):
    from erp.common import backup

    def fake_run(args, **kw):
        assert "capture_output" not in kw
        kw["stdout"].write(b"-- dump")
        return None
    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    p = backup.pg_dump("postgresql+asyncpg://u:p@h/erp", out_dir=str(tmp_path))
    assert open(p, "rb").read() == b"-- dump" and oct(os.stat(p).st_mode)[-3:] == "600"


def test_job_backup_pushes_attachments_offsite(monkeypatch, tmp_path):
    """오프사이트 — DB 파일뿐 아니라 첨부 백업본도 전송."""
    import asyncio

    from erp.common import backup
    from erp.config import get_settings
    from erp.workers import scheduler
    s = get_settings()
    att = tmp_path / "att"
    att.mkdir()
    (att / "f").write_bytes(b"x")
    for k, v in {"backup_dir": str(tmp_path / "bk"), "backup_enc_key": KEY,
                 "attachment_dir": str(att), "backup_offsite_dest": "b@h:/p/"}.items():
        monkeypatch.setattr(s, k, v, raising=False)
    monkeypatch.setattr(backup, "pg_dump", lambda url, out_dir=None: b"SQL")
    pushed = []
    monkeypatch.setattr(backup, "push_offsite", lambda p, **kw: pushed.append(os.path.basename(p)))
    asyncio.run(scheduler.job_backup())
    assert len(pushed) == 2 and any("attachments" in p for p in pushed)
