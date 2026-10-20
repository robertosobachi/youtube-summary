#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "youtube-transcript-api>=1.2",
#   "anthropic",
#   "rich",
#   "yt-dlp",
# ]
# ///
"""Usage: yts <youtube-url-or-id>
       yts feed [rec|subs|wl] [-n N]
       yts --clear [youtube-url-or-id]

Needs ANTHROPIC_API_KEY in the environment.
Prints the summary and saves summary and transcript to transcripts/<id>.md next
to this script. Copies a follow-up prompt with both to the clipboard (macOS),
ready to paste into any chat.
feed lists your YouTube Home recommendations (rec, default), Subscriptions
(subs) or Watch Later (wl) using Chrome's cookies, N items (default 20), and
summarises the ones you pick. macOS asks for the keychain password each run.
--clear deletes the saved file for one video, or all of them.
"""

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import anthropic
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text
from youtube_transcript_api import TranscriptsDisabled, YouTubeTranscriptApi

MODEL = "claude-sonnet-5-5"
# Thinking is always on for this model and counts towards max_tokens.
MAX_TOKENS = 16000
EFFORT = "low"
OUT_DIR = Path(__file__).resolve().parent / "transcripts"
# Section separators in the saved file, also used to read the summary back.
SUMMARY = "## Summary\n\n"
TRANSCRIPT = "\n\n## Transcript\n\n"
FEEDS = {"rec": ":ytrec", "subs": ":ytsubs", "wl": ":ytwatchlater"}
BROWSER = ("chrome",)
FEED_SIZE = 20

PROMPT = """Summarise the YouTube transcript you receive.
Write the whole summary, headings included, in the language given on the "Language:" line.

Format:

# <emoji> <title>

## Summary
2-3 sentences.

## Main points
- short bullets

## Key takeaway
One sentence.

Rules: 250 words maximum, no filler, no motivational tone, no inline citations.
Ignore sponsor reads, greetings and requests to subscribe."""


def video_id(arg: str) -> str:
    if re.fullmatch(r"[\w-]{11}", arg):
        return arg
    u = urllib.parse.urlparse(arg)
    if u.hostname and u.hostname.endswith("youtu.be"):
        return u.path.lstrip("/")[:11]
    qs = urllib.parse.parse_qs(u.query)
    if "v" in qs:
        return qs["v"][0]
    m = re.search(r"/(?:shorts|live|embed)/([\w-]{11})", u.path)
    if m:
        return m.group(1)
    sys.exit(f"Can't find a video id in: {arg}")


def video_title(vid: str) -> str:
    url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
        f"https://www.youtube.com/watch?v={vid}"
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            d = json.load(r)
        return f"{d['title']} ({d['author_name']})"
    except Exception:
        return vid


def lang(code: str) -> str:
    return code.split("-")[0].lower()


def transcript(vid: str) -> tuple[str, str]:
    try:
        tracks = list(YouTubeTranscriptApi().list(vid))
    except TranscriptsDisabled:
        sys.exit("This video has no captions, nothing to summarise.")
    if not tracks:
        sys.exit("No transcript available for this video.")
    # Human captions first. Auto-generated tracks include machine dubs listed
    # alphabetically, so the original can't be told apart: prefer English, then Italian.
    track = min(tracks, key=lambda t: (t.is_generated, {"en": 0, "it": 1}.get(lang(t.language_code), 2)))
    text = " ".join(s.text for s in track.fetch()).replace("\n", " ")
    return text, "Italian" if lang(track.language_code) == "it" else "English"


def summarise(vid: str, path: Path) -> str:
    title = video_title(vid)
    text, language = transcript(vid)

    msg = anthropic.Anthropic().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": EFFORT},
        system=PROMPT,
        messages=[{"role": "user", "content": f"Title: {title}\nLanguage: {language}\n\n{text}"}],
    )
    # The response starts with a thinking block; the summary is in the text blocks.
    summary = "".join(b.text for b in msg.content if b.type == "text").strip()
    if not summary:
        sys.exit(f"No summary returned (stop_reason: {msg.stop_reason}).")
    url = f"https://www.youtube.com/watch?v={vid}"
    doc = f"# {title}\n\n{url}\n\n{SUMMARY}{summary}{TRANSCRIPT}{text}\n"
    OUT_DIR.mkdir(exist_ok=True)
    path.write_text(doc)
    print(f"Saved to {path}", file=sys.stderr)
    return summary


def clear(arg: str | None) -> None:
    if arg is None:
        paths = list(OUT_DIR.glob("*.md"))
        for p in paths:
            p.unlink()
        print(f"Removed {len(paths)} cached summaries.", file=sys.stderr)
        return
    vid = video_id(arg)
    path = OUT_DIR / f"{vid}.md"
    if path.exists():
        path.unlink()
        print(f"Removed {path}", file=sys.stderr)
    else:
        print(f"Not cached: {vid}", file=sys.stderr)


def feed_args(args: list[str]) -> tuple[str, int]:
    name, size = "rec", FEED_SIZE
    while args:
        if args[0] in FEEDS:
            name, args = args[0], args[1:]
        elif args[0] == "-n" and len(args) > 1 and args[1].isdigit() and int(args[1]) > 0:
            size, args = int(args[1]), args[2:]
        else:
            sys.exit(__doc__)
    return name, size


def feed_entries(name: str, size: int) -> list[dict]:
    # Imported here so plain `yts <url>` doesn't pay for loading yt-dlp.
    import yt_dlp

    opts = {
        "cookiesfrombrowser": BROWSER,
        "extract_flat": "in_playlist",
        "playlistend": size,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(FEEDS[name], download=False)
    except yt_dlp.utils.DownloadError:
        # yt-dlp has already printed the reason.
        sys.exit("Couldn't load the feed.")
    # Private and deleted videos come back with no channel.
    return [e for e in info.get("entries") or [] if e.get("channel") or e.get("uploader")]


def feed(args: list[str]) -> None:
    entries = feed_entries(*feed_args(args))
    if not entries:
        sys.exit("The feed is empty.")
    out = Console()
    for n, e in enumerate(entries, 1):
        cached = (OUT_DIR / f"{e['id']}.md").exists()
        mark = "*" if cached else " "
        line = f"{n:>3}. {mark} {e['title']}  [{e.get('channel') or e['uploader']}]"
        out.print(Text(line, style="dim" if cached else ""))
    try:
        picked = input("\nPick (e.g. 1 4 7, Enter to quit): ").split()
    except EOFError:
        return
    picks = [entries[int(p) - 1] for p in picked if p.isdigit() and 1 <= int(p) <= len(entries)]
    err = Console(stderr=True)
    for e in picks:
        err.rule(e["title"])
        try:
            show(e["id"], copy=len(picks) == 1)
        except SystemExit as x:
            # summarise() exits on videos without captions; carry on with the rest.
            err.print(str(x.code), style="red")
        except Exception as x:
            err.print(f"Failed: {x}", style="red")
    if len(picks) > 1:
        err.print("\nSeveral videos picked, so nothing copied. `yts <id>` copies the follow-up prompt for one.", style="dim")


def show(vid: str, copy: bool = True) -> None:
    path = OUT_DIR / f"{vid}.md"
    if path.exists():
        # Already summarised: reuse the saved summary, no transcript fetch or API call.
        summary = path.read_text().partition(SUMMARY)[2].partition(TRANSCRIPT)[0]
        print(f"Cached in {path}", file=sys.stderr)
    else:
        summary = summarise(vid, path)
    # Rich drops the colours when stdout isn't a terminal.
    Console().print(Markdown(summary))
    if not copy:
        return
    # Self-contained so it can be pasted into any chat, not only one with file access.
    followup = "Below are the summary and transcript of a YouTube video. Answer my questions about it.\n\n" + path.read_text()
    subprocess.run(["pbcopy"], input=followup.encode(), check=False)
    err = Console(stderr=True)
    err.print()
    err.rule(style="dim")
    err.print("Follow-up prompt (summary and transcript) copied to the clipboard.", style="dim")


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--clear"] and len(args) <= 2:
        clear(args[1] if len(args) == 2 else None)
        return
    if args[:1] == ["feed"]:
        feed(args[1:])
        return
    if len(args) != 1:
        sys.exit(__doc__)
    show(video_id(args[0]))


if __name__ == "__main__":
    main()
