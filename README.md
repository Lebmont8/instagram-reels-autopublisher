# Instagram Reels Autopublisher

A small, production-minded toolkit for publishing scheduled Reels through the official Instagram Graph API.

It provides:

- explicit `dry-run`, container creation, status polling, and publish commands;
- a persistent SQLite scheduling queue;
- duplicate-slot protection and timezone-aware daily scheduling;
- catch-up protection so an outage does not publish a backlog in a burst;
- optional generated cover images;
- weekly Reels insight exports to CSV and JSON;
- no browser automation and no Instagram password handling.

## Requirements

- Python 3.11+
- an Instagram Professional account connected to a Meta app;
- a valid access token with the permissions required by Meta for content publishing;
- public HTTPS URLs for the video and optional cover image;
- FFmpeg/ffprobe for local thumbnail-offset detection;
- Cairo only when using `cover_maker.py`.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python autoposter.py init-db
```

Export the values from `.env` in your preferred process manager. The scripts do not load `.env` automatically except for `weekly_insights.py` when `--env-file` is used.

## Safe first run

Inspect the API request without creating or publishing anything:

```bash
python publish_reel.py dry-run \
  --video-url "https://media.example.com/reel.mp4" \
  --caption "A scheduled test Reel"
```

Create a container without publishing it:

```bash
python publish_reel.py create \
  --video-url "https://media.example.com/reel.mp4" \
  --caption "A scheduled test Reel"
```

Use `status` to wait for processing, then publish explicitly:

```bash
python publish_reel.py status --container-id "CONTAINER_ID"
python publish_reel.py publish --container-id "CONTAINER_ID"
```

## Scheduling

```bash
python autoposter.py add \
  --video-url "https://media.example.com/reel.mp4" \
  --cover-url "https://media.example.com/reel.jpg" \
  --caption "Scheduled through the official API" \
  --scheduled-at "2026-08-17T10:00:00+02:00"

python autoposter.py list
python autoposter.py run-once --dry-run
python autoposter.py run-once
```

`run-once` publishes at most one due Reel by default. Pending posts older than 30 minutes are marked `skipped`, preventing an accidental burst after downtime. Both values are configurable.

Daily slots are available through `schedule-day`:

```bash
python autoposter.py schedule-day \
  --video-url-file video_urls.txt \
  --days-ahead 2 \
  --timezone Europe/Berlin \
  --slots 08:00,12:00,18:00 \
  --skip-existing
```

## Covers

`cover_maker.py` accepts a content package containing scene `image_path` values and produces a 1080x1920 JPEG with safe-zone-aware title placement.

```bash
python cover_maker.py your-package.json -o public/reel-cover.jpg --slug reel-001
```

Set `IG_ASSET_ROOT` when relative image paths should resolve from a directory other than the repository root.

## Insights

```bash
python weekly_insights.py --days 7
```

The command reads published media IDs from the queue and writes a CSV plus a JSON summary. Set `IG_PACKAGE_DIR` to enrich results with local package metadata.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Security and platform boundaries

This project uses the official API only. It does not automate logins, CAPTCHAs, follows, cold mass DMs, scraping, or human-like browser actions. See [SECURITY.md](SECURITY.md).

## License

MIT
