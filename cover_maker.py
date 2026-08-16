#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cairo


ROOT = Path(__file__).resolve().parent
ASSET_ROOT = Path(os.environ.get("IG_ASSET_ROOT", str(ROOT))).expanduser().resolve()
WIDTH = 1080
HEIGHT = 1920
HISTORY_FILE = Path(os.environ.get("IG_COVER_HISTORY", str(ROOT / "cover_history.jsonl"))).expanduser()
RECENT_LOOKBACK = 20
SAFE_X = 150
SAFE_TOP = 520
SAFE_BOTTOM = 520
MAX_TEXT_WIDTH = WIDTH - SAFE_X * 2
MAX_TEXT_HEIGHT = 360
MAX_TEXT_LINES = 3
MAX_FONT_SIZE = 82
MIN_FONT_SIZE = 48

TITLE_BANK = {
    "night_client": ["Customers write at night", "Leads cool by morning", "While your team sleeps"],
    "price_list": ["Customers ask the price", "The price list got lost", "The price is needed now"],
    "delivery_address": ["The address got lost", "Delivery without repeats", "The order needs an address"],
    "angry_customer": ["The customer is upset", "Silence makes it worse", "The chat needs attention"],
    "followup_thinking": ["The customer will think", "The conversation cooled", "Follow up without pressure"],
    "stock_size": ["Is the size available?", "Stock answers matter", "Availability decides"],
    "delivery_time": ["When will it arrive?", "Delivery time decides", "Customers need a date"],
    "booking_slot": ["A customer wants to book", "The slot is available now", "Bookings get lost in chat"],
    "payment_link": ["The customer is ready", "Payment stalled in chat", "Send the payment link"],
    "order_details": ["The order stalled", "The customer wants to buy", "Details arrived in pieces"],
    "warranty_question": ["The customer has doubts", "Warranty builds confidence", "A question before payment"],
    "returns_exchange": ["Can this be exchanged?", "The policy got lost", "Returns without panic"],
    "late_reply": ["The customer is waiting", "Silence lost the lead", "The reply came too late"],
    "wrong_question": ["Route the question", "The customer needs help", "The question has no owner"],
    "discount_request": ["The customer wants a deal", "Negotiation started", "Discounts without chaos"],
}

TITLE_ALIASES = {
    "price_question": "price_list",
    "routing": "wrong_question",
}

FALLBACK_TITLES = [
    "Customers will not wait",
    "The lead cooled in chat",
    "A sale got lost in DMs",
    "The reply is needed now",
    "Make every chat count",
]


def find_font() -> str:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).exists():
            return candidate
    fc_match = shutil.which("fc-match")
    if fc_match:
        result = subprocess.run(
            [fc_match, "-f", "%{file}", "DejaVu Sans"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.stdout.strip():
            return result.stdout.strip()
    raise SystemExit("No DejaVu font found for cover text")


def recent_history(path: Path = HISTORY_FILE) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-RECENT_LOOKBACK:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def choose_title(package: dict, slug: str, history: list[dict]) -> str:
    variant_id = str(package.get("variant_id") or "")
    variant_key = variant_id.removeprefix("uk_")
    variant_key = TITLE_ALIASES.get(variant_key, variant_key)
    candidates = list(TITLE_BANK.get(variant_key) or [])
    scenes = list(package.get("scenes") or [])
    hook = str(package.get("hook") or (scenes[0].get("overlay_text") if scenes else "") or "")
    punchline = next(
        (
            str(scene.get("overlay_text") or "")
            for scene in reversed(scenes)
            if scene.get("scene_function") == "punchline"
        ),
        "",
    )
    for text in (hook, punchline):
        words = re.findall(r"[\w'’]+", text, flags=re.UNICODE)[:7]
        if 3 <= len(words) <= 7:
            candidates.append(" ".join(words))
    candidates = list(dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip()))
    used = {str(row.get("title") or "").casefold() for row in history}
    for group in (candidates, FALLBACK_TITLES):
        if not group:
            continue
        offset = int.from_bytes(hashlib.sha256(f"{slug}:{group[0]}".encode("utf-8")).digest()[:2], "big")
        for index in range(len(group)):
            title = group[(offset + index) % len(group)]
            if title.casefold() not in used:
                return title
        if group is candidates:
            return group[offset % len(group)]
    raise SystemExit("No cover title candidates")


def find_ffmpeg() -> str:
    command = os.environ.get("FFMPEG_BIN", "").strip() or shutil.which("ffmpeg")
    if command:
        return command
    raise SystemExit("ffmpeg not found for cover generation")


def wrap_title(title: str, max_chars: int = 22) -> str:
    words = title.split()
    lines = []
    current = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and len(candidate) > max_chars:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    if len(lines) > 2:
        lines = [lines[0], " ".join(lines[1:])]
    return "\n".join(lines)


def cover_filter(*, font: str, title_file: str) -> str:
    return (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        "drawtext="
        f"fontfile='{font}':textfile='{title_file}':"
        "fontcolor=white:fontsize=88:borderw=9:bordercolor=black:"
        "shadowcolor=black@0.65:shadowx=4:shadowy=5:"
        "line_spacing=22:x=(w-text_w)/2:y=h-text_h-190"
    )


def _text_width(ctx: cairo.Context, text: str) -> float:
    return ctx.text_extents(text).width


def _font_extents(ctx: cairo.Context) -> dict:
    extents = ctx.font_extents()
    if hasattr(extents, "height"):
        return {"ascent": extents.ascent, "descent": extents.descent, "height": extents.height}
    ascent, descent, height, _max_x_advance, _max_y_advance = extents
    return {"ascent": ascent, "descent": descent, "height": height}


def _wrap_by_width(ctx: cairo.Context, title: str, max_width: int) -> list[str]:
    words = title.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or _text_width(ctx, candidate) <= max_width:
            current = candidate
            continue
        lines.append(current)
        current = word
    if current:
        lines.append(current)
    return lines


def layout_title(title: str) -> dict:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    ctx = cairo.Context(surface)
    ctx.select_font_face("DejaVu Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

    for font_size in range(MAX_FONT_SIZE, MIN_FONT_SIZE - 1, -2):
        ctx.set_font_size(font_size)
        lines = _wrap_by_width(ctx, title, MAX_TEXT_WIDTH)
        if len(lines) > MAX_TEXT_LINES:
            continue
        extents = [ctx.text_extents(line) for line in lines]
        font_extents = _font_extents(ctx)
        line_step = int(font_extents["height"] * 1.06)
        total_height = int(font_extents["ascent"] + font_extents["descent"] + line_step * (len(lines) - 1))
        if max((extent.width for extent in extents), default=0) <= MAX_TEXT_WIDTH and total_height <= MAX_TEXT_HEIGHT:
            bottom = HEIGHT - SAFE_BOTTOM
            y = max(SAFE_TOP, bottom - total_height)
            return {
                "lines": lines,
                "font_size": font_size,
                "line_step": line_step,
                "ascent": font_extents["ascent"],
                "descent": font_extents["descent"],
                "x_min": SAFE_X,
                "x_max": WIDTH - SAFE_X,
                "y_min": y,
                "y_max": y + total_height,
            }
    raise RuntimeError(f"Cover title does not fit safe zone: {title}")


def render_title_overlay(*, title: str, output: Path) -> dict:
    layout = layout_title(title)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    ctx = cairo.Context(surface)
    ctx.select_font_face("DejaVu Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    ctx.set_font_size(layout["font_size"])

    baseline_y = layout["y_min"] + layout["ascent"]
    for index, line in enumerate(layout["lines"]):
        extents = ctx.text_extents(line)
        x = (WIDTH - extents.width) / 2 - extents.x_bearing
        y = baseline_y + layout["line_step"] * index

        ctx.save()
        ctx.translate(4, 5)
        ctx.move_to(x, y)
        ctx.text_path(line)
        ctx.set_source_rgba(0, 0, 0, 0.55)
        ctx.set_line_width(max(6, layout["font_size"] * 0.09))
        ctx.stroke()
        ctx.restore()

        ctx.move_to(x, y)
        ctx.text_path(line)
        ctx.set_source_rgba(0, 0, 0, 0.88)
        ctx.set_line_width(max(7, layout["font_size"] * 0.11))
        ctx.stroke_preserve()
        ctx.set_source_rgba(1, 1, 1, 1)
        ctx.fill()

    surface.write_to_png(str(output))
    return layout


def score_cover_scene(scene: dict, index: int, used_images: set[str]) -> int:
    image_path = str(scene.get("image_path") or "")
    if not image_path or image_path in used_images:
        return -10_000
    scene_function = str(scene.get("scene_function") or "")
    emotions = set(scene.get("emotion_tags") or [])
    score = 0
    score += {"hook": 38, "problem": 28, "escalation": 34, "solution": 18, "punchline": 22}.get(scene_function, 10)
    score += int(scene.get("intensity") or 3) * 4
    if emotions & {"shocked_surprised", "angry_grumpy", "panic_screaming", "side_eye", "business_cat"}:
        score += 18
    if index == 0:
        score += 10
    return score


def choose_image(package: dict, history: list[dict]) -> str:
    used_images = {str(row.get("image_path") or "") for row in history}
    scenes = list(package.get("scenes") or [])
    best = max(
        ((score_cover_scene(scene, i, used_images), -i, scene) for i, scene in enumerate(scenes)),
        key=lambda item: (item[0], item[1]),
        default=None,
    )
    if not best or best[0] < -1000:
        best = max(
            ((score_cover_scene(scene, i, set()), -i, scene) for i, scene in enumerate(scenes)),
            key=lambda item: (item[0], item[1]),
            default=None,
        )
    if not best:
        raise SystemExit("No scene image found for cover")
    return str(best[2].get("image_path"))


def escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%")


def make_cover(*, package_path: Path, output: Path, slug: str) -> dict:
    package = json.loads(package_path.read_text(encoding="utf-8"))
    history = recent_history()
    title = choose_title(package, slug, history)
    image_path = Path(choose_image(package, history))
    if not image_path.is_absolute():
        image_path = ASSET_ROOT / image_path
    if not image_path.exists():
        raise SystemExit(f"Cover image not found: {image_path}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", suffix=".png") as overlay_file:
        render_title_overlay(title=title, output=Path(overlay_file.name))
        command = [
            find_ffmpeg(),
            "-y",
            "-i",
            str(image_path),
            "-i",
            overlay_file.name,
            "-filter_complex",
            (
                f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
                f"crop={WIDTH}:{HEIGHT},format=rgba[base];"
                "[1:v]format=rgba[overlay];"
                "[base][overlay]overlay=0:0,format=yuv420p"
            ),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(output),
        ]
        subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not output.is_file() or output.stat().st_size < 20_000:
        raise RuntimeError(f"Cover was not generated correctly: {output}")
    record = {
        "slug": slug,
        "variant_id": package.get("variant_id", ""),
        "title": title,
        "image_path": str(image_path),
        "cover_path": str(output),
    }
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a JPEG cover for a Reel content package.")
    parser.add_argument("package", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--slug", default="")
    args = parser.parse_args()
    slug = args.slug or args.output.stem
    record = make_cover(package_path=args.package, output=args.output, slug=slug)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
