"""SQLite 持久化层。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS inspections (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    total_pieces  INTEGER NOT NULL CHECK (total_pieces >= 0),
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS count_reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id      INTEGER NOT NULL REFERENCES count_reports(id) ON DELETE CASCADE,
    inspection_id  INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    lo             INTEGER NOT NULL CHECK (lo >= 0),
    hi             INTEGER NOT NULL CHECK (hi > lo),
    count          INTEGER NOT NULL CHECK (count >= 0)
);

CREATE TABLE IF NOT EXISTS certificates (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    inspection_id         INTEGER NOT NULL REFERENCES inspections(id) ON DELETE CASCADE,
    qualified_lo          INTEGER NOT NULL CHECK (qualified_lo >= 0),
    qualified_hi          INTEGER NOT NULL CHECK (qualified_hi > qualified_lo),
    committed_count       INTEGER NOT NULL CHECK (committed_count >= 0),
    issued_total_pieces   INTEGER NOT NULL CHECK (issued_total_pieces >= 0),
    issued_min_count      INTEGER NOT NULL CHECK (issued_min_count >= 0),
    status                TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'revoked')),
    issued_at             TEXT NOT NULL,
    revoked_at            TEXT,
    revoked_reason        TEXT,
    revoked_total_pieces  INTEGER,
    revoked_min_count     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_reports_inspection
    ON count_reports(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_inspection
    ON segments(inspection_id);
CREATE INDEX IF NOT EXISTS idx_segments_report
    ON segments(report_id);
CREATE INDEX IF NOT EXISTS idx_certificates_inspection
    ON certificates(inspection_id);
"""

CERTIFICATE_NO_PREFIX = "CERT-"


def format_certificate_no(certificate_id: int) -> str:
    """证明编号：以主键确定性生成，撤销后编号不变。"""
    return f"{CERTIFICATE_NO_PREFIX}{certificate_id:06d}"


def parse_certificate_no(certificate_no: str) -> int | None:
    """解析证明编号；格式不合法返回 ``None``（按不存在处理）。"""
    if not certificate_no.startswith(CERTIFICATE_NO_PREFIX):
        return None
    digits = certificate_no[len(CERTIFICATE_NO_PREFIX) :]
    if not digits.isdigit():
        return None
    return int(digits)


def connect(path: str) -> sqlite3.Connection:
    """打开一个请求级连接。

    ``isolation_level=None`` 让我们显式使用 ``BEGIN IMMEDIATE`` 控制写事务，
    从而把“读取现有计数 + 校验 + 写入”放在同一个串行化事务里。

    ``check_same_thread=False``：每条连接只服务一个请求，但依赖清理与
    端点可能落在不同的线程池线程上；连接不会跨请求共享。
    """
    connection = sqlite3.connect(
        path, timeout=30.0, isolation_level=None, check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_inspection(
    connection: sqlite3.Connection, total_pieces: int, note: str | None
) -> int:
    cursor = connection.execute(
        "INSERT INTO inspections (total_pieces, note, created_at) VALUES (?, ?, ?)",
        (total_pieces, note, now()),
    )
    return int(cursor.lastrowid)


def get_inspection(connection: sqlite3.Connection, inspection_id: int) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count,
               (SELECT COUNT(*) FROM certificates c
                  WHERE c.inspection_id = i.id AND c.status = 'active')
                   AS active_certificate_count,
               (SELECT COUNT(*) FROM certificates c
                  WHERE c.inspection_id = i.id AND c.status = 'revoked')
                   AS revoked_certificate_count
        FROM inspections i
        WHERE i.id = ?
        """,
        (inspection_id,),
    ).fetchone()


def list_inspections(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT i.id, i.total_pieces, i.note, i.created_at,
               (SELECT COUNT(*) FROM count_reports r WHERE r.inspection_id = i.id)
                   AS report_count,
               (SELECT COUNT(*) FROM segments s WHERE s.inspection_id = i.id)
                   AS segment_count,
               (SELECT COUNT(*) FROM certificates c
                  WHERE c.inspection_id = i.id AND c.status = 'active')
                   AS active_certificate_count,
               (SELECT COUNT(*) FROM certificates c
                  WHERE c.inspection_id = i.id AND c.status = 'revoked')
                   AS revoked_certificate_count
        FROM inspections i
        ORDER BY i.id
        """
    ).fetchall()


def update_inspection(
    connection: sqlite3.Connection,
    inspection_id: int,
    total_pieces: int,
    note: str | None,
) -> None:
    connection.execute(
        "UPDATE inspections SET total_pieces = ?, note = ? WHERE id = ?",
        (total_pieces, note, inspection_id),
    )


def load_constraints(
    connection: sqlite3.Connection, inspection_id: int
) -> list[tuple[int, int, int]]:
    rows = connection.execute(
        "SELECT lo, hi, count FROM segments WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    return [(int(row["lo"]), int(row["hi"]), int(row["count"])) for row in rows]


def insert_report(
    connection: sqlite3.Connection,
    inspection_id: int,
    segments: Sequence[tuple[int, int, int]],
) -> int:
    cursor = connection.execute(
        "INSERT INTO count_reports (inspection_id, created_at) VALUES (?, ?)",
        (inspection_id, now()),
    )
    report_id = int(cursor.lastrowid)
    connection.executemany(
        """
        INSERT INTO segments (report_id, inspection_id, lo, hi, count)
        VALUES (?, ?, ?, ?, ?)
        """,
        [(report_id, inspection_id, lo, hi, count) for lo, hi, count in segments],
    )
    return report_id


def get_reports(connection: sqlite3.Connection, inspection_id: int) -> list[dict]:
    reports = connection.execute(
        "SELECT id, inspection_id, created_at FROM count_reports "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()
    segment_rows = connection.execute(
        "SELECT id, report_id, lo, hi, count FROM segments "
        "WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()

    segments_by_report: dict[int, list[dict]] = {int(r["id"]): [] for r in reports}
    for row in segment_rows:
        segments_by_report[int(row["report_id"])].append(
            {
                "id": int(row["id"]),
                "lo": int(row["lo"]),
                "hi": int(row["hi"]),
                "count": int(row["count"]),
            }
        )

    return [
        {
            "id": int(report["id"]),
            "inspection_id": int(report["inspection_id"]),
            "created_at": report["created_at"],
            "segments": segments_by_report[int(report["id"])],
        }
        for report in reports
    ]


def insert_certificate(
    connection: sqlite3.Connection,
    inspection_id: int,
    qualified_lo: int,
    qualified_hi: int,
    committed_count: int,
    issued_total_pieces: int,
    issued_min_count: int,
) -> int:
    """写入一份有效证明，返回证明主键（证明编号由主键确定性生成）。"""
    cursor = connection.execute(
        """
        INSERT INTO certificates (
            inspection_id, qualified_lo, qualified_hi, committed_count,
            issued_total_pieces, issued_min_count, status, issued_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            inspection_id,
            qualified_lo,
            qualified_hi,
            committed_count,
            issued_total_pieces,
            issued_min_count,
            now(),
        ),
    )
    return int(cursor.lastrowid)


def get_certificate(
    connection: sqlite3.Connection, certificate_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM certificates WHERE id = ?",
        (certificate_id,),
    ).fetchone()


def list_certificates(
    connection: sqlite3.Connection, inspection_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM certificates WHERE inspection_id = ? ORDER BY id",
        (inspection_id,),
    ).fetchall()


def list_active_certificates(
    connection: sqlite3.Connection, inspection_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM certificates WHERE inspection_id = ? AND status = 'active' "
        "ORDER BY id",
        (inspection_id,),
    ).fetchall()


def revoke_certificate(
    connection: sqlite3.Connection,
    certificate_id: int,
    revoked_total_pieces: int,
    revoked_min_count: int,
    reason: str,
) -> None:
    """把一份证明置为终态 revoked，并冻结撤销时的总片数与下界快照。

    只对仍有效的证明生效；已撤销证明不会被后续更正改写或恢复。
    """
    connection.execute(
        """
        UPDATE certificates
           SET status = 'revoked',
               revoked_at = ?,
               revoked_reason = ?,
               revoked_total_pieces = ?,
               revoked_min_count = ?
         WHERE id = ? AND status = 'active'
        """,
        (now(), reason, revoked_total_pieces, revoked_min_count, certificate_id),
    )
