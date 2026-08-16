#!/usr/bin/env python3
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import autoposter
import publish_reel


class ReelThumbnailTest(unittest.TestCase):
    def test_middle_thumb_offset_uses_half_of_video_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "reel.mp4"
            video_path.touch()
            completed = mock.Mock(stdout="42.5\n")
            with (
                mock.patch.dict("os.environ", {"IG_PUBLIC_DIR": temp_dir}),
                mock.patch.object(autoposter, "find_ffprobe", return_value="/usr/bin/ffprobe"),
                mock.patch.object(autoposter.subprocess, "run", return_value=completed),
            ):
                offset = autoposter.middle_thumb_offset_ms("https://example.com/media/reel.mp4")

        self.assertEqual(21250, offset)

    def test_middle_thumb_offset_falls_back_when_video_is_not_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict("os.environ", {"IG_PUBLIC_DIR": temp_dir}):
                offset = autoposter.middle_thumb_offset_ms("https://example.com/media/missing.mp4")

        self.assertIsNone(offset)

    def test_cover_url_is_sent_as_reel_cover_without_thumb_offset(self) -> None:
        with mock.patch.object(publish_reel, "request_json", return_value={"id": "container-1"}) as request:
            container_id = publish_reel.create_container(
                "ig-user",
                "token",
                video_url="https://example.com/reel.mp4",
                caption="",
                thumb_offset_ms=25500,
                cover_url="https://example.com/reel.cover.jpg",
            )

        self.assertEqual("container-1", container_id)
        payload = request.call_args.kwargs["data"]
        self.assertEqual("REELS", payload["media_type"])
        self.assertEqual("https://example.com/reel.cover.jpg", payload["cover_url"])
        self.assertNotIn("thumb_offset", payload)


class CatchUpProtectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "posts.sqlite"
        self.conn = autoposter.connect(self.db_path)
        self.now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def add_post(self, *, minutes_ago: int) -> int:
        scheduled_at = (self.now - timedelta(minutes=minutes_ago)).isoformat()
        created_at = (self.now - timedelta(days=1)).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO scheduled_posts
                (video_url, caption, scheduled_at, status, created_at, updated_at)
            VALUES (?, '', ?, 'pending', ?, ?)
            """,
            ("https://example.com/video.mp4", scheduled_at, created_at, created_at),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def test_stale_posts_are_skipped_and_recent_post_is_claimed(self) -> None:
        stale_id = self.add_post(minutes_ago=31)
        recent_id = self.add_post(minutes_ago=5)

        claimed, skipped = autoposter.claim_due_posts(
            self.conn,
            1,
            max_lateness_min=30,
            now=self.now,
        )

        self.assertEqual([recent_id], [int(row["id"]) for row in claimed])
        self.assertEqual([stale_id], skipped)
        statuses = dict(self.conn.execute("SELECT id, status FROM scheduled_posts"))
        self.assertEqual(autoposter.SKIPPED, statuses[stale_id])
        self.assertEqual(autoposter.PROCESSING, statuses[recent_id])

    def test_only_one_recent_post_is_claimed(self) -> None:
        first_id = self.add_post(minutes_ago=10)
        second_id = self.add_post(minutes_ago=5)

        claimed, skipped = autoposter.claim_due_posts(
            self.conn,
            1,
            max_lateness_min=30,
            now=self.now,
        )

        self.assertEqual([], skipped)
        self.assertEqual([first_id], [int(row["id"]) for row in claimed])
        second_status = self.conn.execute(
            "SELECT status FROM scheduled_posts WHERE id = ?",
            (second_id,),
        ).fetchone()["status"]
        self.assertEqual(autoposter.PENDING, second_status)

    def test_exact_lateness_boundary_is_publishable(self) -> None:
        boundary_id = self.add_post(minutes_ago=30)

        claimed, skipped = autoposter.claim_due_posts(
            self.conn,
            1,
            max_lateness_min=30,
            now=self.now,
        )

        self.assertEqual([], skipped)
        self.assertEqual([boundary_id], [int(row["id"]) for row in claimed])


if __name__ == "__main__":
    unittest.main()
