#!/usr/bin/env python3
"""Refresh the README YOUTUBE-VIDEOS-LIST block from the YouTube Data API.

Public, read-only data only. The channel uploads playlist is listed with an
API key (channels.list + playlistItems.list). The README is rewritten only
after a non-empty public video list is returned. A missing API key exits
successfully and leaves the README untouched.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CHANNEL_ID = "UCBRxDSTfr2aJVODDh4WG_7g"
TAG = "YOUTUBE-VIDEOS-LIST"
MAX_VIDEOS = 5
API_ROOT = "https://www.googleapis.com/youtube/v3"
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SKIPPED_TITLES = {"Private video", "Deleted video"}
START_TAG = f"<!-- {TAG}:START -->"
END_TAG = f"<!-- {TAG}:END -->"


@dataclass(frozen=True)
class Video:
    title: str
    video_id: str
    published_at: str


def escape_html(text: str) -> str:
    """Match blog-post-workflow's escapeHTML so titles stay in the same format."""
    return re.sub(
        r"[&<>')(\"]",
        lambda match: {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            "'": "&#39;",
            '"': "&quot;",
            ")": "&rpar;",
            "(": "&lpar;",
        }[match.group(0)],
        text,
    )


def format_item(video: Video) -> str:
    title = escape_html(" ".join(video.title.split()))
    return f"- [{title}](https://www.youtube.com/watch?v={video.video_id})"


def render_readme(content: str, videos: list[Video]) -> str:
    if not videos:
        raise ValueError("refusing to render an empty video list")
    start = content.find(START_TAG)
    end = content.find(END_TAG)
    if start == -1 or end == -1 or end < start:
        raise ValueError(
            f"Cannot find {START_TAG} and {END_TAG} in README.md. "
            "The file was not modified."
        )
    block = "\n" + "\n".join(format_item(video) for video in videos) + "\n"
    return content[: start + len(START_TAG)] + block + content[end:]


def readme_path() -> Path:
    root = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    return root / "README.md"


def warn(message: str) -> None:
    print(f"::warning::{message}")
    print(message, file=sys.stderr)


def fail(message: str) -> None:
    print(f"::error::{message}")
    print(message, file=sys.stderr)
    raise SystemExit(1)


def redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def api_error_detail(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        message = (data.get("error") or {}).get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    detail = " ".join(body.split())
    if len(detail) > 300:
        return detail[:300] + "..."
    return detail or "no response body"


def api_get(path: str, params: dict[str, str], api_key: str) -> dict:
    query = urllib.parse.urlencode(params)
    url = f"{API_ROOT}/{path}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Goog-Api-Key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        body = redact(exc.read().decode("utf-8", errors="replace"), api_key)
        fail(
            f"YouTube Data API {path} returned HTTP {exc.code}. "
            f"README was not modified. {api_error_detail(body)}"
        )
    except urllib.error.URLError as exc:
        fail(
            f"YouTube Data API {path} request failed ({exc.reason}). "
            "README was not modified."
        )
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        fail(
            f"YouTube Data API {path} returned a response that is not JSON. "
            "README was not modified."
        )
    if not isinstance(data, dict):
        fail(
            f"YouTube Data API {path} returned an unexpected payload. "
            "README was not modified."
        )
    return data


def fetch_latest_videos(api_key: str) -> list[Video]:
    channel = api_get(
        "channels",
        {"part": "contentDetails", "id": CHANNEL_ID},
        api_key,
    )
    items = channel.get("items") or []
    if not items:
        fail(
            f"YouTube Data API found no channel {CHANNEL_ID}. "
            "README was not modified."
        )
    try:
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except (KeyError, TypeError, IndexError):
        fail(
            "YouTube Data API response did not include the uploads playlist. "
            "README was not modified."
        )

    listing = api_get(
        "playlistItems",
        {
            "part": "snippet",
            "playlistId": uploads,
            "maxResults": "15",
        },
        api_key,
    )
    videos: list[Video] = []
    for item in listing.get("items") or []:
        snippet = item.get("snippet") or {}
        resource = snippet.get("resourceId") or {}
        video_id = resource.get("videoId") or ""
        title = (snippet.get("title") or "").strip()
        if resource.get("kind") not in (None, "youtube#video"):
            continue
        if not VIDEO_ID_RE.fullmatch(video_id):
            continue
        if not title or title in SKIPPED_TITLES:
            continue
        videos.append(
            Video(
                title=title,
                video_id=video_id,
                published_at=snippet.get("publishedAt") or "",
            )
        )
    videos.sort(key=lambda video: video.published_at, reverse=True)
    return videos[:MAX_VIDEOS]


def update_readme(videos: list[Video]) -> bool:
    path = readme_path()
    if not path.is_file():
        fail(f"{path} does not exist. README was not modified.")
    with path.open(encoding="utf-8", newline="") as handle:
        original = handle.read()
    try:
        updated = render_readme(original, videos)
    except ValueError as exc:
        fail(str(exc))
    if updated == original:
        print(f"{TAG} is already up to date ({len(videos)} videos).")
        return False
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(f"Updated {TAG} with {len(videos)} videos.")
    return True


def run_self_test() -> None:
    assert escape_html('A & B (C) "d" <e>') == (
        "A &amp; B &lpar;C&rpar; &quot;d&quot; &lt;e&gt;"
    )
    sample = (
        "before\n"
        f"{START_TAG}\n"
        "- [old](https://www.youtube.com/watch?v=aaaaaaaaaaa)\n"
        f"{END_TAG} \n"
        "after\n"
    )
    videos = [
        Video("Tailwind CSS in Rust?! Topcoat Assets, Icons & Fonts", "lBDLBfzpUSE", "2"),
        Video("Building with AI in 2026 ft. Brandon Roberts (AnalogJS)", "AQiZ6kHHm7c", "1"),
    ]
    rendered = render_readme(sample, videos)
    expected = (
        "before\n"
        f"{START_TAG}\n"
        "- [Tailwind CSS in Rust?! Topcoat Assets, Icons &amp; Fonts]"
        "(https://www.youtube.com/watch?v=lBDLBfzpUSE)\n"
        "- [Building with AI in 2026 ft. Brandon Roberts &lpar;AnalogJS&rpar;]"
        "(https://www.youtube.com/watch?v=AQiZ6kHHm7c)\n"
        f"{END_TAG} \n"
        "after\n"
    )
    assert rendered == expected, rendered
    try:
        render_readme("no markers", videos)
    except ValueError:
        pass
    else:
        raise AssertionError("missing markers should fail")
    try:
        render_readme(sample, [])
    except ValueError:
        pass
    else:
        raise AssertionError("empty list should fail")
    print("self-test ok")


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        run_self_test()
        return 0

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not api_key:
        warn(
            "YOUTUBE_API_KEY repository secret is not set. "
            "Skipped the README update; the existing YOUTUBE-VIDEOS-LIST section "
            "was left unchanged. Add the secret, then re-run Latest YouTube Videos."
        )
        return 0

    videos = fetch_latest_videos(api_key)
    if not videos:
        fail(
            "YouTube Data API returned no public videos. "
            "README was not modified."
        )
    update_readme(videos)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
