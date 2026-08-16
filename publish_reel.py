#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_GRAPH_VERSION = "v25.0"
FINISHED_STATUS = "FINISHED"
TERMINAL_ERROR_STATUSES = {"ERROR", "EXPIRED"}
DEFAULT_REELS_SHARE_TO_FEED = False
DEFAULT_REELS_THUMB_OFFSET_MS = 3000


class InstagramApiError(RuntimeError):
    pass


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise SystemExit(f"Missing required env var: {name}")
    return value


def graph_base() -> str:
    host = os.environ.get("IG_GRAPH_HOST", "graph.instagram.com").strip("/")
    version = os.environ.get("IG_GRAPH_VERSION", DEFAULT_GRAPH_VERSION).strip("/")
    return f"https://{host}/{version}"


def parse_bool(value: str | bool | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise SystemExit(f"Invalid boolean value: {value}")


def reels_share_to_feed_default() -> bool:
    return parse_bool(os.environ.get("IG_REELS_SHARE_TO_FEED"), default=DEFAULT_REELS_SHARE_TO_FEED)


def reels_thumb_offset_default() -> int | None:
    raw = os.environ.get("IG_REELS_THUMB_OFFSET_MS")
    if raw is None or raw.strip() == "":
        return DEFAULT_REELS_THUMB_OFFSET_MS
    value = int(raw)
    return value if value >= 0 else None


def request_json(method: str, path: str, *, token: str, data: dict | None = None) -> dict:
    url = f"{graph_base()}/{path.lstrip('/')}"
    body = None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_payload = exc.read().decode("utf-8", errors="replace")
        raise InstagramApiError(f"HTTP {exc.code}: {error_payload}") from exc
    except urllib.error.URLError as exc:
        raise InstagramApiError(str(exc)) from exc

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InstagramApiError(f"Non-JSON response: {payload[:500]}") from exc


def create_container(
    ig_user_id: str,
    token: str,
    *,
    video_url: str,
    caption: str,
    share_to_feed: bool | None = None,
    thumb_offset_ms: int | None = None,
    cover_url: str = "",
) -> str:
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
    }
    if caption:
        payload["caption"] = caption
    payload["share_to_feed"] = reels_share_to_feed_default() if share_to_feed is None else share_to_feed
    if cover_url:
        payload["cover_url"] = cover_url
    elif thumb_offset_ms is None:
        thumb_offset_ms = reels_thumb_offset_default()
    if thumb_offset_ms is not None and not cover_url:
        payload["thumb_offset"] = int(thumb_offset_ms)
    response = request_json("POST", f"{ig_user_id}/media", token=token, data=payload)
    container_id = response.get("id")
    if not container_id:
        raise InstagramApiError(f"No container id in response: {response}")
    return str(container_id)


def get_status(container_id: str, token: str) -> dict:
    fields = urllib.parse.urlencode({"fields": "status_code"})
    return request_json("GET", f"{container_id}?{fields}", token=token)


def wait_until_ready(container_id: str, token: str, *, timeout_sec: int, poll_sec: int) -> dict:
    deadline = time.time() + timeout_sec
    last = {}
    while time.time() < deadline:
        last = get_status(container_id, token)
        status = str(last.get("status_code", ""))
        if status == FINISHED_STATUS:
            return last
        if status in TERMINAL_ERROR_STATUSES:
            raise InstagramApiError(f"Container status {status}: {last}")
        time.sleep(poll_sec)
    raise InstagramApiError(f"Timed out waiting for container. Last status: {last}")


def publish_container(ig_user_id: str, token: str, *, container_id: str) -> str:
    response = request_json("POST", f"{ig_user_id}/media_publish", token=token, data={"creation_id": container_id})
    media_id = response.get("id")
    if not media_id:
        raise InstagramApiError(f"No media id in response: {response}")
    return str(media_id)


def get_limit(ig_user_id: str, token: str) -> dict:
    return request_json("GET", f"{ig_user_id}/content_publishing_limit", token=token)


def load_caption(args: argparse.Namespace) -> str:
    if args.caption_file:
        return open(args.caption_file, encoding="utf-8").read().strip()
    return args.caption or ""


def add_reels_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--share-to-feed", dest="share_to_feed", action="store_true")
    group.add_argument("--no-share-to-feed", dest="share_to_feed", action="store_false")
    parser.set_defaults(share_to_feed=None)
    parser.add_argument("--thumb-offset-ms", type=int, default=None)
    parser.add_argument("--cover-url", default="")


def print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_dry_run(args: argparse.Namespace) -> None:
    caption = load_caption(args)
    print_json(
        {
            "mode": "dry-run",
            "steps": [
                "POST /<IG_ID>/media media_type=REELS video_url caption",
                "GET /<IG_CONTAINER_ID>?fields=status_code",
                "POST /<IG_ID>/media_publish creation_id",
            ],
            "video_url": args.video_url,
            "caption": caption,
            "share_to_feed": reels_share_to_feed_default() if args.share_to_feed is None else args.share_to_feed,
            "thumb_offset_ms": args.thumb_offset_ms if args.thumb_offset_ms is not None else reels_thumb_offset_default(),
            "cover_url": args.cover_url,
            "will_publish": False,
        }
    )


def cmd_check_env(_args: argparse.Namespace) -> None:
    ig_user_id = env("IG_USER_ID")
    token = env("IG_ACCESS_TOKEN")
    print_json(
        {
            "ok": True,
            "ig_user_id": ig_user_id,
            "graph_base": graph_base(),
            "token_present": bool(token),
            "token_preview": f"{token[:4]}...{token[-4:]}" if len(token) >= 12 else "***",
        }
    )


def cmd_limit(_args: argparse.Namespace) -> None:
    print_json(get_limit(env("IG_USER_ID"), env("IG_ACCESS_TOKEN")))


def cmd_create(args: argparse.Namespace) -> None:
    container_id = create_container(
        env("IG_USER_ID"),
        env("IG_ACCESS_TOKEN"),
        video_url=args.video_url,
        caption=load_caption(args),
        share_to_feed=args.share_to_feed,
        thumb_offset_ms=args.thumb_offset_ms,
        cover_url=args.cover_url,
    )
    print_json({"container_id": container_id, "published": False})


def cmd_status(args: argparse.Namespace) -> None:
    print_json(get_status(args.container_id, env("IG_ACCESS_TOKEN")))


def cmd_publish(args: argparse.Namespace) -> None:
    media_id = publish_container(env("IG_USER_ID"), env("IG_ACCESS_TOKEN"), container_id=args.container_id)
    print_json({"media_id": media_id, "published": True})


def cmd_full(args: argparse.Namespace) -> None:
    ig_user_id = env("IG_USER_ID")
    token = env("IG_ACCESS_TOKEN")
    container_id = create_container(
        ig_user_id,
        token,
        video_url=args.video_url,
        caption=load_caption(args),
        share_to_feed=args.share_to_feed,
        thumb_offset_ms=args.thumb_offset_ms,
        cover_url=args.cover_url,
    )
    status = wait_until_ready(container_id, token, timeout_sec=args.timeout_sec, poll_sec=args.poll_sec)
    if not args.yes_publish:
        print_json({"container_id": container_id, "status": status, "published": False, "next": f"python3 instagram/publish_reel.py publish --container-id {container_id}"})
        return
    media_id = publish_container(ig_user_id, token, container_id=container_id)
    print_json({"container_id": container_id, "media_id": media_id, "published": True})


def add_caption_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--caption", default="")
    parser.add_argument("--caption-file", default="")


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish Instagram Reels through the official Instagram Platform API.")
    sub = parser.add_subparsers(dest="command", required=True)

    dry = sub.add_parser("dry-run")
    dry.add_argument("--video-url", required=True)
    add_caption_args(dry)
    add_reels_args(dry)
    dry.set_defaults(func=cmd_dry_run)

    check = sub.add_parser("check-env")
    check.set_defaults(func=cmd_check_env)

    limit = sub.add_parser("limit")
    limit.set_defaults(func=cmd_limit)

    create = sub.add_parser("create")
    create.add_argument("--video-url", required=True)
    add_caption_args(create)
    add_reels_args(create)
    create.set_defaults(func=cmd_create)

    status = sub.add_parser("status")
    status.add_argument("--container-id", required=True)
    status.set_defaults(func=cmd_status)

    publish = sub.add_parser("publish")
    publish.add_argument("--container-id", required=True)
    publish.set_defaults(func=cmd_publish)

    full = sub.add_parser("full")
    full.add_argument("--video-url", required=True)
    full.add_argument("--timeout-sec", type=int, default=900)
    full.add_argument("--poll-sec", type=int, default=15)
    full.add_argument("--yes-publish", action="store_true")
    add_caption_args(full)
    add_reels_args(full)
    full.set_defaults(func=cmd_full)

    args = parser.parse_args()
    try:
        args.func(args)
    except InstagramApiError as exc:
        print(f"Instagram API error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
