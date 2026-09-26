"""数据快照：为整夜自测提供可回滚点（对话数据按仓库约定不入 git，故单独快照）。

用法：
    python tools/snapshot_data.py            # 做一次快照
    python tools/snapshot_data.py --list     # 看有哪些快照
    python tools/snapshot_data.py --restore <快照目录>   # 回滚（会先自动快照当前状态）

用 sqlite3 在线备份 API，避免直接复制正在写入的 .db / -wal 得到半截数据。
"""

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
BACKUP_ROOT = ROOT.parent / "_overnight_backup"
KEEP = 40  # 只留最近 40 份，每份约 150KB

FILES_TO_COPY = [
    BACKEND / "runtime_model_config.json",
    ROOT / ".env",
]
DIRS_TO_COPY = [BACKEND / "avatars"]


def _verify(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        conn.close()
    if ok != "ok":
        raise RuntimeError(f"快照校验失败: {ok}")
    return ",".join(sorted(tables))


def snapshot(label: str = "") -> Path:
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_ROOT / f"{stamp}{('-' + label) if label else ''}"
    target.mkdir(parents=True, exist_ok=False)

    src = BACKEND / "moz.db"
    dst = target / "moz.db"
    if src.exists():
        s = sqlite3.connect(src)
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        tables = _verify(dst)
    else:
        tables = ""

    for f in FILES_TO_COPY:
        if f.exists():
            shutil.copy2(f, target / f.name)
    for dr in DIRS_TO_COPY:
        if dr.exists():
            shutil.copytree(dr, target / dr.name)

    meta = {
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "db_bytes": dst.stat().st_size if dst.exists() else 0,
        "tables": tables,
    }
    (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    _prune()
    return target


def _prune() -> None:
    dirs = sorted([p for p in BACKUP_ROOT.iterdir() if p.is_dir()])
    for old in dirs[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def list_snapshots() -> list:
    if not BACKUP_ROOT.exists():
        return []
    return sorted([p.name for p in BACKUP_ROOT.iterdir() if p.is_dir()])


def restore(name: str) -> Path:
    src = BACKUP_ROOT / name
    if not (src / "moz.db").exists():
        raise SystemExit(f"快照 {name} 里没有 moz.db")
    safety = snapshot("pre-restore")
    shutil.copy2(src / "moz.db", BACKEND / "moz.db")
    for wal in (BACKEND / "moz.db-wal", BACKEND / "moz.db-shm"):
        wal.unlink(missing_ok=True)  # 旧 WAL 配新库会读出脏数据
    cfg = src / "runtime_model_config.json"
    if cfg.exists():
        shutil.copy2(cfg, BACKEND / "runtime_model_config.json")
    av = src / "avatars"
    if av.exists():
        shutil.rmtree(BACKEND / "avatars", ignore_errors=True)
        shutil.copytree(av, BACKEND / "avatars")
    print(f"已回滚到 {name}；回滚前的状态存进了 {safety.name}")
    return src


def restart_backend() -> None:
    """回滚后必须重启后端：它持有旧连接和内存缓存。"""
    print("提示：请重启后端（uvicorn --reload 会自动重启；否则手动结束 8000 端口进程）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore")
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    if a.list:
        for n in list_snapshots():
            print(n)
        return 0
    if a.restore:
        restore(a.restore)
        restart_backend()
        return 0
    p = snapshot(a.label)
    meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
    print(f"快照 {p.name}: {meta['db_bytes']} bytes, {len(meta['tables'].split(','))} 张表")
    return 0


if __name__ == "__main__":
    sys.exit(main())
