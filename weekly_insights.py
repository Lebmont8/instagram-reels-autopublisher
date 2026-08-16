#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PACKAGE_DIR = Path(os.environ.get("IG_PACKAGE_DIR", str(ROOT / "packages"))).expanduser()
DEFAULT_ENV_FILE = ROOT / ".env"
DEFAULT_DB = ROOT / "posts.sqlite"
DEFAULT_DAYS = 7
DEFAULT_GRAPH_VERSION = "v25.0"
DEFAULT_72H_SNAPSHOT = ROOT / "insights_72h_snapshots.json"
REELS_METRICS = (
    "reach",
    "views",
    "likes",
    "comments",
    "saved",
    "shares",
    "total_interactions",
    "ig_reels_avg_watch_time",
    "ig_reels_video_view_total_time",
    "reels_skip_rate",
)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def graph_base() -> str:
    host = os.environ.get("IG_GRAPH_HOST", "graph.instagram.com").strip("/")
    version = os.environ.get("IG_GRAPH_VERSION", DEFAULT_GRAPH_VERSION).strip("/")
    return f"https://{host}/{version}"


def request_json(path: str, params: dict[str, str]) -> dict:
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        raise SystemExit("Missing IG_ACCESS_TOKEN")
    payload = dict(params)
    payload["access_token"] = token
    url = f"{graph_base()}/{path.lstrip('/')}?{urllib.parse.urlencode(payload)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Instagram API HTTP {exc.code}: {body}") from exc


def compact_insights(data: dict) -> dict[str, int | float | None]:
    result = {}
    for item in data.get("data", []):
        value = None
        if isinstance(item.get("total_value"), dict):
            value = item["total_value"].get("value")
        if value is None and item.get("values"):
            value = item["values"][0].get("value")
        result[item.get("name")] = value
    return result


def slug_from_url(url: str) -> str:
    filename = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
    return filename[:-4] if filename.endswith(".mp4") else filename


def music_label(record: dict) -> str:
    title = record.get("music_title")
    author = record.get("music_author")
    if title and author:
        return f"{title} — {author}"
    if title:
        return str(title)
    if record.get("ig_audio_name"):
        return str(record["ig_audio_name"])
    if record.get("music_file"):
        return str(record["music_file"])
    if record.get("music_id"):
        return f"track {record['music_id']}"
    return "unknown"


def load_package_meta(slug: str) -> dict:
    for suffix in (".cat_dialogue.json", ".chat_screenshot.json", ".owner_pov.json", ".with_images.json"):
        path = PACKAGE_DIR / f"{slug}{suffix}"
        if not path.exists():
            continue
        try:
            package = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        music = package.get("music") or {}
        return {
            "format": package.get("template") or suffix.strip(".json"),
            "variant_id": package.get("variant_id") or package.get("title"),
            "title": package.get("title"),
            "hook": package.get("hook"),
            "scene_count": len(package.get("scenes") or []),
            "music": music,
            "music_id": music.get("id"),
            "music_file": music.get("file"),
            "music_title": music.get("title"),
            "music_author": music.get("author"),
            "music_profiles": ",".join(music.get("profiles") or []),
            "music_energy": music.get("energy"),
            "music_dialogue_friendly": music.get("dialogue_friendly"),
            "music_selection_score": music.get("selection_score"),
            "music_selection_reason": "; ".join(music.get("selection_reason") or []),
            "experiment_id": package.get("experiment_id"),
            "ab_version": package.get("ab_version"),
            "hook_type": package.get("hook_type"),
            "duration_target": package.get("duration_target"),
            "scene_count": package.get("scene_count") or len(package.get("scenes") or []),
            "cast_id": package.get("cast_id"),
            "visual_style": package.get("visual_style"),
            "music_test": package.get("music_test"),
            "slot_test": package.get("slot_test"),
            "bot_action": package.get("bot_action"),
            "test_variable": package.get("test_variable"),
            "test_setting": package.get("test_setting"),
        }
    return {}


def slot_from_label(label: str | None) -> str:
    match = re.search(r"(\d\d:\d\d)", label or "")
    return match.group(1) if match else ""


def fetch_media_meta(media_id: str) -> dict:
    fields = "id,media_product_type,timestamp,permalink,audio_name"
    try:
        return request_json(media_id, {"fields": fields})
    except RuntimeError as exc:
        if "audio_name" not in str(exc):
            raise
        return request_json(media_id, {"fields": "id,media_product_type,timestamp,permalink"})


def fetch_records(db_path: Path, days: int) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    metrics = ",".join(REELS_METRICS)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, media_id, published_at, slot_label, source, video_url
            FROM scheduled_posts
            WHERE status = 'published'
              AND media_id IS NOT NULL
              AND published_at >= ?
            ORDER BY published_at DESC
            """,
            (cutoff,),
        ).fetchall()

    records = []
    for row in rows:
        meta = fetch_media_meta(row["media_id"])
        insights = request_json(f"{row['media_id']}/insights", {"metric": metrics})
        slug = slug_from_url(row["video_url"])
        record = {
            "local_id": row["id"],
            "media_id": row["media_id"],
            "published_at": row["published_at"],
            "slot": slot_from_label(row["slot_label"]),
            "slot_label": row["slot_label"],
            "source": row["source"],
            "slug": slug,
            "permalink": meta.get("permalink"),
            "ig_audio_name": meta.get("audio_name"),
        }
        record.update(load_package_meta(slug))
        record["music_label"] = music_label(record)
        record.update(compact_insights(insights))
        reach = record.get("reach") or 0
        duration_target = float(record.get("duration_target") or 0)
        avg_watch_ms = record.get("ig_reels_avg_watch_time")
        record["watch_ratio"] = (
            round(float(avg_watch_ms) / (duration_target * 1000), 4)
            if avg_watch_ms is not None and duration_target > 0
            else None
        )
        record["likes_per_reach"] = round((record.get("likes") or 0) / reach, 6) if reach else 0
        record["saves_per_reach"] = round((record.get("saved") or 0) / reach, 6) if reach else 0
        record["shares_per_reach"] = round((record.get("shares") or 0) / reach, 6) if reach else 0
        records.append(record)
    return records


def capture_72h_snapshots(
    records: list[dict],
    path: Path = DEFAULT_72H_SNAPSHOT,
    *,
    now: datetime | None = None,
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    payload = {"version": 1, "snapshots": []}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {"version": 1, "snapshots": []}
    snapshots = payload.get("snapshots") or []
    by_slug = {item.get("slug"): item for item in snapshots if item.get("slug")}
    changed = False
    for record in records:
        slug = record.get("slug")
        if not slug:
            continue
        published_raw = str(record.get("published_at") or "")
        try:
            published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        age_hours = (now - published.astimezone(timezone.utc)).total_seconds() / 3600
        record["age_hours"] = round(age_hours, 1)
        existing = by_slug.get(slug)
        if existing:
            record["reach_72h"] = existing.get("reach_72h")
            record["views_72h"] = existing.get("views_72h")
            continue
        if 72 <= age_hours <= 120:
            snapshot = {
                "slug": slug,
                "published_at": published_raw,
                "captured_at": now.isoformat(),
                "age_hours": round(age_hours, 1),
                "reach_72h": record.get("reach") or 0,
                "views_72h": record.get("views") or 0,
                "experiment_id": record.get("experiment_id"),
                "ab_version": record.get("ab_version"),
            }
            snapshots.append(snapshot)
            by_slug[slug] = snapshot
            record["reach_72h"] = snapshot["reach_72h"]
            record["views_72h"] = snapshot["views_72h"]
            changed = True
    if changed:
        path.write_text(
            json.dumps({"version": 1, "snapshots": snapshots}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return records


def metric_buckets(records: list[dict], key_name: str) -> list[dict]:
    stats = defaultdict(
        lambda: {
            "count": 0,
            "reach": 0,
            "views": 0,
            "interactions": 0,
            "watch_sum": 0,
            "watch_n": 0,
            "music_energy": None,
            "music_profiles": "",
        }
    )
    for record in records:
        key = record.get(key_name) or "unknown"
        bucket = stats[key]
        bucket["count"] += 1
        bucket["reach"] += record.get("reach") or 0
        bucket["views"] += record.get("views") or 0
        bucket["interactions"] += sum(
            record.get(metric) or 0
            for metric in ("likes", "comments", "saved", "shares")
        )
        if record.get("ig_reels_avg_watch_time") is not None:
            bucket["watch_sum"] += record.get("ig_reels_avg_watch_time") or 0
            bucket["watch_n"] += 1
        if key_name == "music_label":
            bucket["music_energy"] = bucket["music_energy"] or record.get("music_energy")
            bucket["music_profiles"] = bucket["music_profiles"] or record.get("music_profiles") or ""

    rows = []
    for name, values in stats.items():
        count = values["count"] or 1
        views = values["views"] or 0
        rows.append(
            {
                "name": name,
                "count": values["count"],
                "total_reach": values["reach"],
                "avg_reach": round(values["reach"] / count, 1),
                "total_views": views,
                "avg_views": round(views / count, 1),
                "engagement_rate": round(values["interactions"] / views * 100, 2) if views else 0,
                "avg_watch_ms": round(values["watch_sum"] / values["watch_n"]) if values["watch_n"] else None,
                "music_energy": values["music_energy"],
                "music_profiles": values["music_profiles"],
            }
        )
    return sorted(rows, key=lambda row: (row["avg_views"], row["avg_reach"]), reverse=True)


def summarize(records: list[dict], *, top_n: int) -> dict:
    top_reach = sorted(records, key=lambda item: (item.get("reach") or 0, item.get("views") or 0), reverse=True)[:top_n]
    top_watch = sorted(
        [item for item in records if (item.get("views") or 0) > 0],
        key=lambda item: item.get("ig_reels_avg_watch_time") or 0,
        reverse=True,
    )[:top_n]

    slot_stats = defaultdict(lambda: {"count": 0, "reach": 0, "views": 0, "watch_sum": 0, "watch_n": 0})
    format_stats = defaultdict(lambda: {"count": 0, "reach": 0, "views": 0, "watch_sum": 0, "watch_n": 0})
    variant_stats = defaultdict(lambda: {"count": 0, "reach": 0, "views": 0, "watch_sum": 0, "watch_n": 0})
    experiment_stats = defaultdict(
        lambda: {
            "count": 0,
            "reach": 0,
            "views": 0,
            "watch_ratio_sum": 0.0,
            "watch_ratio_n": 0,
            "likes": 0,
            "saved": 0,
            "shares": 0,
            "reach_72h_sum": 0.0,
            "reach_72h_n": 0,
        }
    )
    for record in records:
        for key, bucket in (
            (record.get("slot") or "unknown", slot_stats),
            (record.get("format") or "unknown", format_stats),
            (record.get("variant_id") or "unknown", variant_stats),
        ):
            stats = bucket[key]
            stats["count"] += 1
            stats["reach"] += record.get("reach") or 0
            stats["views"] += record.get("views") or 0
            if record.get("ig_reels_avg_watch_time") is not None:
                stats["watch_sum"] += record.get("ig_reels_avg_watch_time") or 0
                stats["watch_n"] += 1
        experiment_key = f"{record.get('experiment_id') or 'unknown'}:{record.get('ab_version') or '?'}"
        stats = experiment_stats[experiment_key]
        stats["count"] += 1
        stats["reach"] += record.get("reach") or 0
        stats["views"] += record.get("views") or 0
        stats["likes"] += record.get("likes") or 0
        stats["saved"] += record.get("saved") or 0
        stats["shares"] += record.get("shares") or 0
        if record.get("watch_ratio") is not None:
            stats["watch_ratio_sum"] += record["watch_ratio"]
            stats["watch_ratio_n"] += 1
        if record.get("reach_72h") is not None:
            stats["reach_72h_sum"] += record["reach_72h"]
            stats["reach_72h_n"] += 1

    def rows_from_stats(stats: dict) -> list[dict]:
        rows = []
        for name, values in stats.items():
            count = values["count"] or 1
            rows.append(
                {
                    "name": name,
                    "count": values["count"],
                    "total_reach": values["reach"],
                    "avg_reach": round(values["reach"] / count, 1),
                    "total_views": values["views"],
                    "avg_views": round(values["views"] / count, 1),
                    "avg_watch_ms": round(values["watch_sum"] / values["watch_n"]) if values["watch_n"] else None,
                }
            )
        return sorted(rows, key=lambda row: (row["avg_reach"], row["avg_views"]), reverse=True)

    experiment_rows = []
    for name, values in experiment_stats.items():
        reach = values["reach"] or 0
        count = values["count"] or 1
        experiment_rows.append(
            {
                "name": name,
                "count": values["count"],
                "avg_reach": round(reach / count, 1),
                "avg_views": round(values["views"] / count, 1),
                "avg_watch_ratio": (
                    round(values["watch_ratio_sum"] / values["watch_ratio_n"], 4)
                    if values["watch_ratio_n"]
                    else None
                ),
                "likes_per_reach": round(values["likes"] / reach, 6) if reach else 0,
                "saves_per_reach": round(values["saved"] / reach, 6) if reach else 0,
                "shares_per_reach": round(values["shares"] / reach, 6) if reach else 0,
                "shares_saves_total": values["saved"] + values["shares"],
                "avg_reach_72h": (
                    round(values["reach_72h_sum"] / values["reach_72h_n"], 1)
                    if values["reach_72h_n"]
                    else None
                ),
                "reach_72h_count": values["reach_72h_n"],
            }
        )

    def relative_winner(a_value, b_value, threshold: float) -> str | None:
        if a_value is None or b_value is None:
            return None
        a_value = float(a_value)
        b_value = float(b_value)
        if a_value == b_value:
            return None
        lower = min(a_value, b_value)
        higher = max(a_value, b_value)
        if lower == 0:
            return "A" if a_value > b_value else "B"
        if (higher - lower) / lower < threshold:
            return None
        return "A" if a_value > b_value else "B"

    by_experiment: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in experiment_rows:
        experiment_id, version = row["name"].rsplit(":", 1)
        by_experiment[experiment_id][version] = row

    experiment_decisions = []
    for experiment_id, versions in sorted(by_experiment.items()):
        a_row = versions.get("A")
        b_row = versions.get("B")
        decision = {
            "experiment_id": experiment_id,
            "status": "insufficient_data",
            "winner": None,
            "passed_metrics": [],
        }
        if not a_row or not b_row or a_row["count"] < 3 or b_row["count"] < 3:
            experiment_decisions.append(decision)
            continue

        metric_winners = {
            "watch_ratio": relative_winner(
                a_row["avg_watch_ratio"], b_row["avg_watch_ratio"], 0.15
            ),
            "shares_saves_per_reach": (
                relative_winner(
                    a_row["saves_per_reach"] + a_row["shares_per_reach"],
                    b_row["saves_per_reach"] + b_row["shares_per_reach"],
                    0.50,
                )
                if a_row["shares_saves_total"] + b_row["shares_saves_total"] >= 2
                else None
            ),
            "reach_72h": (
                relative_winner(a_row["avg_reach_72h"], b_row["avg_reach_72h"], 0.20)
                if a_row["reach_72h_count"] >= 3 and b_row["reach_72h_count"] >= 3
                else None
            ),
        }
        wins = defaultdict(list)
        for metric, winner in metric_winners.items():
            if winner:
                wins[winner].append(metric)
        winning_version, passed_metrics = max(
            wins.items(), key=lambda item: len(item[1]), default=(None, [])
        )
        if winning_version and len(passed_metrics) >= 2:
            decision.update(
                status="winner",
                winner=winning_version,
                passed_metrics=passed_metrics,
            )
        else:
            decision["status"] = "inconclusive"
        experiment_decisions.append(decision)

    return {
        "count": len(records),
        "top_reach": top_reach,
        "top_watch": top_watch,
        "slot_summary": rows_from_stats(slot_stats),
        "format_summary": rows_from_stats(format_stats),
        "variant_summary": rows_from_stats(variant_stats),
        "experiment_summary": sorted(experiment_rows, key=lambda row: row["name"]),
        "experiment_decisions": experiment_decisions,
        "music_summary": metric_buckets(records, "music_label"),
    }


def write_csv(path: Path, records: list[dict]) -> None:
    columns = [
        "local_id",
        "published_at",
        "slot",
        "format",
        "variant_id",
        "title",
        "hook",
        "scene_count",
        "experiment_id",
        "ab_version",
        "hook_type",
        "duration_target",
        "cast_id",
        "visual_style",
        "music_test",
        "slot_test",
        "bot_action",
        "test_variable",
        "test_setting",
        "music_label",
        "ig_audio_name",
        "music_id",
        "music_title",
        "music_author",
        "music_file",
        "music_profiles",
        "music_energy",
        "music_dialogue_friendly",
        "music_selection_score",
        "music_selection_reason",
        "reach",
        "views",
        "likes",
        "comments",
        "saved",
        "shares",
        "total_interactions",
        "ig_reels_avg_watch_time",
        "ig_reels_video_view_total_time",
        "reels_skip_rate",
        "watch_ratio",
        "likes_per_reach",
        "saves_per_reach",
        "shares_per_reach",
        "age_hours",
        "reach_72h",
        "views_72h",
        "permalink",
        "slug",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect weekly Instagram Reels insights.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("IG_POSTS_DB", str(DEFAULT_DB))))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--csv", type=Path, default=ROOT / "insights_week_latest.csv")
    parser.add_argument("--json", type=Path, default=ROOT / "insights_week_summary.json")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    load_env_file(args.env_file)
    db_path = Path(os.environ.get("IG_POSTS_DB", str(args.db)))
    records = fetch_records(db_path, args.days)
    capture_72h_snapshots(records)
    summary = summarize(records, top_n=args.top)
    write_csv(args.csv, records)
    payload = {
        "ok": True,
        "days": args.days,
        "csv": str(args.csv),
        "summary_json": str(args.json),
        **summary,
    }
    args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
