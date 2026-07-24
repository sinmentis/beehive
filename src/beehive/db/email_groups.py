"""Periodic digest emails are organized into custom groups (see schema.sql's email_groups /
email_group_channels tables) instead of a single fixed once-daily send. Each group owns its own
recipient, subject template, and send cadence; a Channel (editorial or monitor, either kind)
belongs to at most one group at a time. assign_channel() is therefore always a *move*: it drops
any previous membership row before inserting the new one, mirroring the DB-level guarantee from
email_group_channels.channel_id being UNIQUE."""
from __future__ import annotations

import sqlite3


def create_email_group(conn: sqlite3.Connection, name: str, subject_template: str = "",
                        recipient_email: str | None = None,
                        send_interval_hours: int = 24, *,
                        schedule_mode: str = "interval",
                        schedule_timezone: str = "Pacific/Auckland",
                        schedule_time: str = "09:00",
                        schedule_weekdays: str = "0,1,2,3,4,5,6") -> int:
    cur = conn.execute(
        """
        INSERT INTO email_groups (
            name, subject_template, recipient_email, send_interval_hours,
            schedule_mode, schedule_timezone, schedule_time, schedule_weekdays
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            subject_template,
            recipient_email or None,
            send_interval_hours,
            schedule_mode,
            schedule_timezone,
            schedule_time,
            schedule_weekdays,
        ),
    )
    conn.commit()
    return cur.lastrowid


def get_email_group(conn: sqlite3.Connection, group_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM email_groups WHERE id = ?", (group_id,)).fetchone()
    return dict(row) if row else None


def list_email_groups(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM email_groups ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def update_email_group(conn: sqlite3.Connection, group_id: int, name: str,
                        subject_template: str, recipient_email: str | None,
                        send_interval_hours: int, *,
                        schedule_mode: str = "interval",
                        schedule_timezone: str = "Pacific/Auckland",
                        schedule_time: str = "09:00",
                        schedule_weekdays: str = "0,1,2,3,4,5,6") -> None:
    conn.execute(
        """
        UPDATE email_groups
        SET name = ?,
            subject_template = ?,
            recipient_email = ?,
            send_interval_hours = ?,
            schedule_mode = ?,
            schedule_timezone = ?,
            schedule_time = ?,
            schedule_weekdays = ?
        WHERE id = ?
        """,
        (
            name,
            subject_template,
            recipient_email or None,
            send_interval_hours,
            schedule_mode,
            schedule_timezone,
            schedule_time,
            schedule_weekdays,
            group_id,
        ),
    )
    conn.commit()


def delete_email_group(conn: sqlite3.Connection, group_id: int) -> None:
    conn.execute("DELETE FROM email_groups WHERE id = ?", (group_id,))
    conn.commit()


def assign_channel(conn: sqlite3.Connection, group_id: int, channel_id: int) -> None:
    """Moves channel_id into group_id, leaving whatever group (if any) it previously belonged
    to -- a Channel is only ever a member of one group at a time."""
    conn.execute("DELETE FROM email_group_channels WHERE channel_id = ?", (channel_id,))
    conn.execute(
        "INSERT INTO email_group_channels (email_group_id, channel_id) VALUES (?, ?)",
        (group_id, channel_id))
    conn.commit()


def unassign_channel(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute("DELETE FROM email_group_channels WHERE channel_id = ?", (channel_id,))
    conn.commit()


def list_member_channels(conn: sqlite3.Connection, group_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT channels.* FROM channels "
        "JOIN email_group_channels ON email_group_channels.channel_id = channels.id "
        "WHERE email_group_channels.email_group_id = ? ORDER BY channels.id",
        (group_id,)).fetchall()
    return [dict(r) for r in rows]


def get_channel_group(conn: sqlite3.Connection, channel_id: int) -> dict | None:
    row = conn.execute(
        "SELECT email_groups.* FROM email_groups "
        "JOIN email_group_channels ON email_group_channels.email_group_id = email_groups.id "
        "WHERE email_group_channels.channel_id = ?",
        (channel_id,)).fetchone()
    return dict(row) if row else None


def mark_sent(conn: sqlite3.Connection, group_id: int, sent_at: str) -> None:
    conn.execute(
        """
        UPDATE email_groups
        SET last_sent_at = ?,
            last_checked_at = ?,
            last_error = NULL,
            last_error_at = NULL
        WHERE id = ?
        """,
        (sent_at, sent_at, group_id),
    )
    conn.commit()


def mark_checked(conn: sqlite3.Connection, group_id: int, checked_at: str) -> None:
    conn.execute(
        """
        UPDATE email_groups
        SET last_checked_at = ?,
            last_error = NULL,
            last_error_at = NULL
        WHERE id = ?
        """,
        (checked_at, group_id),
    )
    conn.commit()


def mark_error(
    conn: sqlite3.Connection,
    group_id: int,
    *,
    error: str,
    failed_at: str,
) -> None:
    conn.execute(
        """
        UPDATE email_groups
        SET last_error = ?, last_error_at = ?
        WHERE id = ?
        """,
        (error, failed_at, group_id),
    )
    conn.commit()
