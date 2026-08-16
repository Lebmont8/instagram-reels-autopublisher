#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from publish_reel import (
    InstagramApiError,
    create_container,
    env,
    publish_container,
    wait_until_ready,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "posts.sqlite"
DEFAULT_CAPTION_TEMPLATE = ROOT / "caption_template.txt"
DEFAULT_PUBLIC_DIR = ROOT / "public"
FFPROBE_OVERRIDE = os.environ.get("FFPROBE_BIN", "").strip()
DEFAULT_SLOTS = (
    "08:00",
    "09:30",
    "11:00",
    "12:00",
    "13:30",
    "15:00",
    "17:00",
    "19:00",
    "21:00",
    "23:00",
)
DEFAULT_SCHEDULE_TZ = "Europe/Berlin"
PENDING = "pending"
PROCESSING = "processing"
PUBLISHED = "published"
FAILED = "failed"
SKIPPED = "skipped"
ACTIVE_STATUSES = (PENDING, PROCESSING)
DEFAULT_MAX_LATENESS_MIN = 30


def find_ffprobe() -> str | None:
    if FFPROBE_OVERRIDE:
        override = Path(FFPROBE_OVERRIDE).expanduser()
        if override.is_file():
            return str(override)
        resolved = shutil.which(FFPROBE_OVERRIDE)
        if resolved:
            return resolved
    return shutil.which("ffprobe")


def local_public_video_path(video_url: str) -> Path | None:
    filename = Path(urllib.parse.unquote(urllib.parse.urlsplit(video_url).path)).name
    if not filename.lower().endswith(".mp4"):
        return None

    public_dir = Path(os.environ.get("IG_PUBLIC_DIR", str(DEFAULT_PUBLIC_DIR))).resolve()
    candidate = (public_dir / filename).resolve()
    try:
        candidate.relative_to(public_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def middle_thumb_offset_ms(video_url: str) -> int | None:
    video_path = local_public_video_path(video_url)
    ffprobe = find_ffprobe()
    if video_path is None or ffprobe is None:
        return None

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        duration_sec = float(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    if duration_sec <= 0:
        return None
    return int(duration_sec * 1000 / 2)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_scheduled_at(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise SystemExit("scheduled_at is required")
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(
            "Invalid --scheduled-at. Use ISO format, for example: 2026-06-03T10:00:00+02:00"
        ) from exc
    if parsed.tzinfo is None:
        raise SystemExit("Invalid --scheduled-at: timezone is required, for example +02:00")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_local_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit("Invalid date. Use YYYY-MM-DD, for example: 2026-06-04") from exc


def parse_slot(value: str) -> dt_time:
    raw = value.strip()
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
    except ValueError as exc:
        raise SystemExit(f"Invalid slot '{value}'. Use HH:MM, for example: 08:00") from exc
    return parsed


def parse_slots(value: str) -> list[str]:
    slots = [item.strip() for item in value.split(",") if item.strip()]
    if not slots:
        raise SystemExit("At least one slot is required")
    for slot in slots:
        parse_slot(slot)
    return slots


def local_slot_to_utc_iso(local_day: date, slot: str, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise SystemExit(f"Invalid timezone: {tz_name}") from exc
    local_dt = datetime.combine(local_day, parse_slot(slot), tzinfo=tz)
    return local_dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def default_target_day(days_ahead: int, tz_name: str) -> date:
    try:
        tz = ZoneInfo(tz_name)
    except Exception as exc:
        raise SystemExit(f"Invalid timezone: {tz_name}") from exc
    return datetime.now(tz).date() + timedelta(days=days_ahead)


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_url TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            container_id TEXT,
            media_id TEXT,
            cover_url TEXT,
            error TEXT,
            slot_label TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT
        )
        """
    )
    ensure_column(conn, "scheduled_posts", "slot_label", "TEXT")
    ensure_column(conn, "scheduled_posts", "source", "TEXT")
    ensure_column(conn, "scheduled_posts", "cover_url", "TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduled_posts_due ON scheduled_posts(status, scheduled_at)"
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_posts_active_slot
        ON scheduled_posts(scheduled_at)
        WHERE status IN ('pending', 'processing')
        """
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def row_to_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_caption(args: argparse.Namespace) -> str:
    if args.caption_file:
        return Path(args.caption_file).read_text(encoding="utf-8").strip()
    return args.caption or ""


def load_caption_template(args: argparse.Namespace) -> str:
    caption = load_caption(args)
    if caption:
        return caption
    template_path = Path(args.caption_template)
    if template_path.exists():
        return template_path.read_text(encoding="utf-8").strip()
    return ""


def load_video_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.video_url or [])
    if args.video_url_file:
        lines = Path(args.video_url_file).read_text(encoding="utf-8").splitlines()
        urls.extend(line.strip() for line in lines if line.strip() and not line.strip().startswith("#"))
    clean_urls = []
    seen = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        clean_urls.append(url)
    return clean_urls


def cmd_init_db(args: argparse.Namespace) -> None:
    with connect(Path(args.db)) as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM scheduled_posts").fetchone()["count"]
    print_json({"ok": True, "db": args.db, "posts": count})


def cmd_add(args: argparse.Namespace) -> None:
    scheduled_at = parse_scheduled_at(args.scheduled_at)
    now = utc_now_iso()
    with connect(Path(args.db)) as conn:
        cur = conn.execute(
            """
            INSERT INTO scheduled_posts
                (video_url, caption, scheduled_at, status, slot_label, source, cover_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (args.video_url, load_caption(args), scheduled_at, PENDING, args.slot_label, args.source, args.cover_url, now, now),
        )
        conn.commit()
        post = conn.execute(
            "SELECT * FROM scheduled_posts WHERE id = ?",
            (cur.lastrowid,),
        ).fetchone()
    print_json({"added": row_to_dict(post)})


def cmd_list(args: argparse.Namespace) -> None:
    query = "SELECT * FROM scheduled_posts"
    params: list[object] = []
    if args.status:
        query += " WHERE status = ?"
        params.append(args.status)
    query += " ORDER BY scheduled_at ASC, id ASC LIMIT ?"
    params.append(args.limit)
    with connect(Path(args.db)) as conn:
        rows = conn.execute(query, params).fetchall()
    print_json([row_to_dict(row) for row in rows])


def due_window(now: datetime, max_lateness_min: int) -> tuple[str, str]:
    if max_lateness_min < 0:
        raise ValueError("max_lateness_min must be 0 or greater")
    current = now.astimezone(timezone.utc).replace(microsecond=0)
    oldest = current - timedelta(minutes=max_lateness_min)
    return oldest.isoformat(), current.isoformat()


def skip_stale_posts(conn: sqlite3.Connection, *, oldest_due: str, now: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT id
        FROM scheduled_posts
        WHERE status = ? AND scheduled_at < ?
        ORDER BY scheduled_at ASC, id ASC
        """,
        (PENDING, oldest_due),
    ).fetchall()
    ids = [int(row["id"]) for row in rows]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""
            UPDATE scheduled_posts
            SET status = ?, error = ?, updated_at = ?
            WHERE id IN ({placeholders}) AND status = ?
            """,
            (
                SKIPPED,
                "Missed publication window; automatic catch-up disabled",
                now,
                *ids,
                PENDING,
            ),
        )
        conn.commit()
    return ids


def claim_due_posts(
    conn: sqlite3.Connection,
    limit: int,
    *,
    max_lateness_min: int = DEFAULT_MAX_LATENESS_MIN,
    now: datetime | None = None,
) -> tuple[list[sqlite3.Row], list[int]]:
    oldest_due, current = due_window(now or datetime.now(timezone.utc), max_lateness_min)
    skipped_ids = skip_stale_posts(conn, oldest_due=oldest_due, now=current)
    rows = conn.execute(
        """
        SELECT * FROM scheduled_posts
        WHERE status = ? AND scheduled_at >= ? AND scheduled_at <= ?
        ORDER BY scheduled_at ASC, id ASC
        LIMIT ?
        """,
        (PENDING, oldest_due, current, limit),
    ).fetchall()
    for row in rows:
        conn.execute(
            """
            UPDATE scheduled_posts
            SET status = ?, updated_at = ?, error = NULL
            WHERE id = ? AND status = ?
            """,
            (PROCESSING, current, row["id"], PENDING),
        )
    conn.commit()
    return rows, skipped_ids


def mark_failed(conn: sqlite3.Connection, post_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE scheduled_posts
        SET status = ?, error = ?, updated_at = ?
        WHERE id = ?
        """,
        (FAILED, error[:2000], utc_now_iso(), post_id),
    )
    conn.commit()


def mark_published(
    conn: sqlite3.Connection,
    post_id: int,
    *,
    container_id: str,
    media_id: str,
) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE scheduled_posts
        SET status = ?, container_id = ?, media_id = ?, error = NULL, updated_at = ?, published_at = ?
        WHERE id = ?
        """,
        (PUBLISHED, container_id, media_id, now, now, post_id),
    )
    conn.commit()


def publish_post(post: sqlite3.Row, *, ig_user_id: str, token: str, timeout_sec: int, poll_sec: int) -> dict:
    container_id = create_container(
        ig_user_id,
        token,
        video_url=post["video_url"],
        caption=post["caption"],
        thumb_offset_ms=middle_thumb_offset_ms(post["video_url"]),
        cover_url=post["cover_url"] or "",
    )
    wait_until_ready(container_id, token, timeout_sec=timeout_sec, poll_sec=poll_sec)
    media_id = publish_container(ig_user_id, token, container_id=container_id)
    return {"container_id": container_id, "media_id": media_id}


def cmd_run_once(args: argparse.Namespace) -> None:
    oldest_due, current = due_window(datetime.now(timezone.utc), args.max_lateness_min)
    if args.dry_run:
        with connect(Path(args.db)) as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_posts
                WHERE status = ? AND scheduled_at >= ? AND scheduled_at <= ?
                ORDER BY scheduled_at ASC, id ASC
                LIMIT ?
                """,
                (PENDING, oldest_due, current, args.limit),
            ).fetchall()
            stale_rows = conn.execute(
                """
                SELECT * FROM scheduled_posts
                WHERE status = ? AND scheduled_at < ?
                ORDER BY scheduled_at ASC, id ASC
                """,
                (PENDING, oldest_due),
            ).fetchall()
        print_json(
            {
                "dry_run": True,
                "due": [row_to_dict(row) for row in rows],
                "would_skip_as_stale": [row_to_dict(row) for row in stale_rows],
                "max_lateness_min": args.max_lateness_min,
            }
        )
        return

    ig_user_id = env("IG_USER_ID")
    token = env("IG_ACCESS_TOKEN")
    results = []
    with connect(Path(args.db)) as conn:
        posts, skipped_ids = claim_due_posts(
            conn,
            args.limit,
            max_lateness_min=args.max_lateness_min,
        )
        for post in posts:
            try:
                published = publish_post(
                    post,
                    ig_user_id=ig_user_id,
                    token=token,
                    timeout_sec=args.timeout_sec,
                    poll_sec=args.poll_sec,
                )
            except InstagramApiError as exc:
                mark_failed(conn, int(post["id"]), str(exc))
                results.append({"id": post["id"], "status": FAILED, "error": str(exc)})
                continue
            mark_published(
                conn,
                int(post["id"]),
                container_id=published["container_id"],
                media_id=published["media_id"],
            )
            results.append({"id": post["id"], "status": PUBLISHED, **published})
    print_json({"processed": results, "skipped_stale_ids": skipped_ids})


def cmd_reset_failed(args: argparse.Namespace) -> None:
    now = utc_now_iso()
    with connect(Path(args.db)) as conn:
        cur = conn.execute(
            """
            UPDATE scheduled_posts
            SET status = ?, error = NULL, updated_at = ?
            WHERE status = ?
            """,
            (PENDING, now, FAILED),
        )
        conn.commit()
    print_json({"reset": cur.rowcount})


def active_future_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM scheduled_posts
        WHERE status IN ('pending', 'processing') AND scheduled_at > ?
        """,
        (utc_now_iso(),),
    ).fetchone()["count"]


def slot_is_free(conn: sqlite3.Connection, scheduled_at: str) -> bool:
    row = conn.execute(
        """
        SELECT id
        FROM scheduled_posts
        WHERE scheduled_at = ? AND status IN ('pending', 'processing')
        LIMIT 1
        """,
        (scheduled_at,),
    ).fetchone()
    return row is None


def schedule_video(
    conn: sqlite3.Connection,
    *,
    video_url: str,
    caption: str,
    cover_url: str,
    scheduled_at: str,
    slot_label: str,
    source: str,
) -> sqlite3.Row:
    now = utc_now_iso()
    cur = conn.execute(
        """
        INSERT INTO scheduled_posts
            (video_url, caption, scheduled_at, status, slot_label, source, cover_url, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (video_url, caption, scheduled_at, PENDING, slot_label, source, cover_url, now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM scheduled_posts WHERE id = ?", (cur.lastrowid,)).fetchone()


def cmd_schedule_day(args: argparse.Namespace) -> None:
    video_urls = load_video_urls(args)
    cover_urls = list(args.cover_url or [])
    slots = parse_slots(args.slots)
    if len(video_urls) != len(slots):
        raise SystemExit(f"Need exactly {len(slots)} video URLs for slots {', '.join(slots)}")
    if cover_urls and len(cover_urls) != len(video_urls):
        raise SystemExit(f"Need exactly {len(video_urls)} cover URLs, got {len(cover_urls)}")

    target_day = parse_local_date(args.date) if args.date else default_target_day(args.days_ahead, args.timezone)
    caption = load_caption_template(args)
    caption_items = list(args.caption_item or [])
    if caption_items and len(caption_items) != len(video_urls):
        raise SystemExit(f"Need exactly {len(video_urls)} item captions, got {len(caption_items)}")
    scheduled = []
    skipped = []
    with connect(Path(args.db)) as conn:
        for index, (video_url, slot) in enumerate(zip(video_urls, slots)):
            cover_url = cover_urls[index] if cover_urls else ""
            item_caption = caption_items[index] if caption_items else caption
            scheduled_at = local_slot_to_utc_iso(target_day, slot, args.timezone)
            if not slot_is_free(conn, scheduled_at):
                if args.skip_existing:
                    skipped.append(
                        {
                            "video_url": video_url,
                            "slot_label": f"{target_day.isoformat()} {slot} {args.timezone}",
                            "reason": "slot_occupied",
                        }
                    )
                    continue
                raise SystemExit(f"Slot is already occupied: {target_day.isoformat()} {slot} {args.timezone}")
            post = schedule_video(
                conn,
                video_url=video_url,
                caption=item_caption,
                cover_url=cover_url,
                scheduled_at=scheduled_at,
                slot_label=f"{target_day.isoformat()} {slot} {args.timezone}",
                source=args.source,
            )
            scheduled.append(row_to_dict(post))
    print_json({"scheduled": scheduled, "skipped": skipped})


def cmd_buffer_status(args: argparse.Namespace) -> None:
    slots = parse_slots(args.slots)
    today = default_target_day(0, args.timezone)
    until = today + timedelta(days=args.days)
    rows_by_day: dict[str, list[dict]] = {}
    with connect(Path(args.db)) as conn:
        count = active_future_count(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM scheduled_posts
            WHERE status IN ('pending', 'processing') AND scheduled_at > ?
            ORDER BY scheduled_at ASC, id ASC
            """,
            (utc_now_iso(),),
        ).fetchall()
    for row in rows:
        scheduled_dt = datetime.fromisoformat(row["scheduled_at"]).astimezone(ZoneInfo(args.timezone))
        day = scheduled_dt.date().isoformat()
        if today <= scheduled_dt.date() <= until:
            rows_by_day.setdefault(day, []).append(row_to_dict(row))
    print_json(
        {
            "active_future_posts": count,
            "min_scheduled": args.min_scheduled,
            "ok": count >= args.min_scheduled,
            "timezone": args.timezone,
            "slots": slots,
            "days": rows_by_day,
        }
    )


def add_db_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=os.environ.get("IG_POSTS_DB", str(DEFAULT_DB)))


def add_caption_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--caption", default="")
    parser.add_argument("--caption-file", default="")


def add_schedule_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timezone", default=os.environ.get("IG_SCHEDULE_TZ", DEFAULT_SCHEDULE_TZ))
    parser.add_argument("--slots", default=os.environ.get("IG_DAILY_SLOTS", ",".join(DEFAULT_SLOTS)))
    parser.add_argument("--caption-template", default=os.environ.get("IG_CAPTION_TEMPLATE", str(DEFAULT_CAPTION_TEMPLATE)))
    parser.add_argument("--source", default="")


def main() -> None:
    parser = argparse.ArgumentParser(description="Scheduled Instagram Reels autoposter.")
    add_db_arg(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init_db = sub.add_parser("init-db")
    init_db.set_defaults(func=cmd_init_db)

    add = sub.add_parser("add")
    add.add_argument("--video-url", required=True)
    add.add_argument("--cover-url", default="")
    add.add_argument("--scheduled-at", required=True)
    add.add_argument("--slot-label", default="")
    add.add_argument("--source", default="")
    add_caption_args(add)
    add.set_defaults(func=cmd_add)

    schedule_day = sub.add_parser("schedule-day")
    schedule_day.add_argument("--video-url", action="append", default=[])
    schedule_day.add_argument("--cover-url", action="append", default=[])
    schedule_day.add_argument("--video-url-file", default="")
    schedule_day.add_argument("--caption-item", action="append", default=[])
    schedule_day.add_argument("--date", default="")
    schedule_day.add_argument("--days-ahead", type=int, default=2)
    schedule_day.add_argument("--skip-existing", action="store_true")
    add_caption_args(schedule_day)
    add_schedule_args(schedule_day)
    schedule_day.set_defaults(func=cmd_schedule_day)

    buffer_status = sub.add_parser("buffer-status")
    buffer_status.add_argument("--min-scheduled", type=int, default=10)
    buffer_status.add_argument("--days", type=int, default=7)
    add_schedule_args(buffer_status)
    buffer_status.set_defaults(func=cmd_buffer_status)

    list_posts = sub.add_parser("list")
    list_posts.add_argument("--status", default="")
    list_posts.add_argument("--limit", type=int, default=50)
    list_posts.set_defaults(func=cmd_list)

    run_once = sub.add_parser("run-once")
    run_once.add_argument("--limit", type=int, default=1)
    run_once.add_argument(
        "--max-lateness-min",
        type=int,
        default=int(os.environ.get("IG_MAX_LATENESS_MIN", DEFAULT_MAX_LATENESS_MIN)),
        help="Skip pending posts older than this many minutes instead of publishing a catch-up burst.",
    )
    run_once.add_argument("--timeout-sec", type=int, default=900)
    run_once.add_argument("--poll-sec", type=int, default=15)
    run_once.add_argument("--dry-run", action="store_true")
    run_once.set_defaults(func=cmd_run_once)

    reset_failed = sub.add_parser("reset-failed")
    reset_failed.set_defaults(func=cmd_reset_failed)

    args = parser.parse_args()
    try:
        args.func(args)
    except sqlite3.Error as exc:
        print(f"SQLite error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
