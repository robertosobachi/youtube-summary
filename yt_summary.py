#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "youtube-transcript-api>=1.2",
#   "anthropic",
#   "rich",
#   "yt-dlp",
#   "pillow",
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
summarises the ones you pick. Up to two entries marked 🌟 are Claude's guess at
what you'd want, based on what you summarised before. macOS asks for the
keychain password each run.
--clear deletes the saved file for one video, or all of them.
"""

import base64
import io
import json
import os
import re
import select
import subprocess
import sys
import termios
import tty
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
# Cheap model that highlights feed entries resembling what was summarised before.
PICK_MODEL = "claude-haiku-5-5"
PICK_HISTORY = 50
# Height in terminal rows of the thumbnail shown under the feed picker.
PREVIEW_ROWS = 9
# Thumbnail next to each feed entry: 16:9 over two rows of roughly 1:2 cells.
ICON_ROWS, ICON_COLS = 2, 7
ICON_PX = (128, 72)
# Column the icon starts at, after the selection mark and the number.
ICON_AT = 8

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


def history_titles() -> list[str]:
    paths = sorted(OUT_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    titles = []
    for p in paths[:PICK_HISTORY]:
        with p.open() as f:
            titles.append(f.readline().removeprefix("# ").strip())
    return titles


def recommend(entries: list[dict]) -> set[str]:
    history = history_titles()
    fresh = [e for e in entries if not (OUT_DIR / f"{e['id']}.md").exists()]
    # With no history the pick would be a guess, so don't spend a call on it.
    if not history or not fresh:
        return set()
    listed = "\n".join(f"{n}. {e['title']} [{e.get('channel') or e['uploader']}]" for n, e in enumerate(fresh, 1))
    prompt = (
        "Videos I chose to summarise before, newest first:\n"
        + "\n".join(f"- {t}" for t in history)
        + f"\n\nNew videos:\n{listed}\n\n"
        "Which 1 or 2 new videos am I most likely to want? Reply with their numbers only."
    )
    try:
        msg = anthropic.Anthropic().messages.create(
            model=PICK_MODEL,
            max_tokens=4000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        # A highlight isn't worth blocking the feed for.
        return set()
    text = "".join(b.text for b in msg.content if b.type == "text")
    picks = [int(n) for n in re.findall(r"\d+", text) if 1 <= int(n) <= len(fresh)]
    return {fresh[n - 1]["id"] for n in picks[:2]}


def can_draw_images() -> bool:
    # Terminals that speak the Kitty graphics protocol. Warp has no iTerm2 inline images.
    return (
        os.environ.get("TERM_PROGRAM") in ("WarpTerminal", "ghostty")
        or os.environ.get("TERM") == "xterm-kitty"
        or "KITTY_WINDOW_ID" in os.environ
    )


def thumbnail(vid: str) -> tuple[str, str] | None:
    """The video's thumbnail as base64 PNG, which is all Kitty's f=100 takes:
    full size for the preview and shrunk for the icon, so redraws stay light."""
    from PIL import Image

    def b64(img: Image.Image) -> str:
        png = io.BytesIO()
        img.save(png, "PNG")
        return base64.b64encode(png.getvalue()).decode()

    try:
        with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", timeout=5) as r:
            img = Image.open(io.BytesIO(r.read()))
            img.load()
    except Exception:
        # A missing thumbnail isn't worth blocking the feed for.
        return None
    return b64(img), b64(img.resize(ICON_PX))


def kitty_image(data: str, image_id: int, rows: int, cols: int | None = None) -> str:
    """Escape that draws an image at the cursor without moving it, sent in 4096-byte chunks."""
    chunks = [data[i : i + 4096] for i in range(0, len(data), 4096)]
    # With only the height set, the width keeps the aspect ratio.
    head = f"a=T,f=100,i={image_id},r={rows},{f'c={cols},' if cols else ''}C=1,q=2,"
    return "".join(
        f"\x1b_G{head if n == 0 else ''}m={int(n < len(chunks) - 1)};{c}\x1b\\" for n, c in enumerate(chunks)
    )


def kitty_delete(image_id: int) -> str:
    return f"\x1b_Ga=d,d=I,i={image_id},q=2\x1b\\"


def feed_item(n: int, e: dict, recs: set[str], icon: bool = False) -> list[Text]:
    """The entry's rows in the list: one, or with an icon, the title over the channel
    with room on the left for the icon."""
    cached = (OUT_DIR / f"{e['id']}.md").exists()
    rec = e["id"] in recs
    num = (f"{n:>3}. ", "dim")
    # Both emoji are two columns wide; the blank keeps titles aligned.
    mark = "✅" if cached else "🌟" if rec else "  "
    title = (e["title"], "bold magenta" if rec else "bold")
    channel = (f"[{e.get('channel') or e['uploader']}]", "cyan")
    if icon:
        gap = " " * (ICON_COLS + 1)
        rows = [Text.assemble(num, gap, mark, " ", title), Text.assemble(" " * 5, gap, "   ", channel)]
    else:
        rows = [Text.assemble(num, mark, " ", title, "  ", channel)]
    if cached:
        for row in rows:
            row.stylize("dim")
    return rows


def pick(items: list[list[Text]], posters: list[tuple[str, str] | None] | None = None) -> list[int]:
    """Arrow-key picker on stderr. With posters, each entry gets its icon and the
    highlighted one a larger preview under the list. Returns the chosen indices, empty on quit."""
    err = Console(stderr=True)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    cur, top, drawn, chosen, shown = 0, 0, 0, set(), []
    height = len(items[0])
    preview = PREVIEW_ROWS if posters else 0
    # Leave room for the hint, the line the cursor ends on and the preview.
    size = max(1, min(len(items), (err.height - 2 - preview) // height))
    hint = Text("↑↓ move  space select  enter summarise  q quit", style="dim")

    def draw() -> None:
        nonlocal top, drawn, shown
        top = min(max(top, cur - size + 1), cur)
        if drawn:
            err.file.write(f"\x1b[{drawn}F\x1b[J")
        err.file.write("".join(kitty_delete(i) for i in shown))
        shown = []
        for i in range(top, top + size):
            for k, line in enumerate(items[i]):
                # One terminal line per row, or the redraw lands in the wrong place.
                row = Text.assemble("● " if i in chosen and k == 0 else "  ", line, no_wrap=True, overflow="ellipsis")
                if i == cur:
                    row.stylize("reverse")
                err.print(row)
            if posters and posters[i]:
                # Back up to the entry's first row, draw the icon in the gap, come back down.
                icon = kitty_image(posters[i][1], i + 2, height, ICON_COLS)
                err.file.write(f"\x1b[{height}A\x1b[{ICON_AT}G{icon}\x1b[{height}B\r")
                shown.append(i + 2)
        if posters:
            if posters[cur]:
                err.file.write(kitty_image(posters[cur][0], 1, PREVIEW_ROWS))
                shown.append(1)
            err.file.write("\n" * preview)
        err.print(hint)
        drawn = size * height + 1 + preview

    try:
        tty.setcbreak(fd)
        err.file.write("\x1b[?25l")
        while True:
            draw()
            key = os.read(fd, 1)
            # A lone Esc quits; an arrow key sends Esc followed by more bytes.
            if key == b"\x1b" and select.select([fd], [], [], 0.05)[0]:
                key += os.read(fd, 8)
            if key in (b"\x1b[A", b"k"):
                cur = max(cur - 1, 0)
            elif key in (b"\x1b[B", b"j"):
                cur = min(cur + 1, len(items) - 1)
            elif key == b" ":
                chosen ^= {cur}
            elif key in (b"\r", b"\n"):
                return sorted(chosen) or [cur]
            elif key in (b"\x1b", b"q"):
                return []
    except KeyboardInterrupt:
        return []
    finally:
        if drawn:
            err.file.write(f"\x1b[{drawn}F\x1b[J")
        err.file.write("".join(kitty_delete(i) for i in shown))
        err.file.write("\x1b[?25h")
        err.file.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def feed(args: list[str]) -> None:
    entries = feed_entries(*feed_args(args))
    if not entries:
        sys.exit("The feed is empty.")
    interactive = sys.stdin.isatty() and sys.stderr.isatty()
    with Console(stderr=True).status("Picking..."), ThreadPoolExecutor(8) as pool:
        # Thumbnails load while Haiku picks, so moving through the list never waits on the network.
        thumbs = pool.map(thumbnail, [e["id"] for e in entries]) if interactive and can_draw_images() else None
        recs = recommend(entries)
        posters = list(thumbs) if thumbs else None
    # Icons and preview need room for at least three entries.
    if not any(posters or []) or Console(stderr=True).height - 2 - PREVIEW_ROWS < 3 * ICON_ROWS:
        posters = None
    items = [feed_item(n, e, recs, icon=bool(posters)) for n, e in enumerate(entries, 1)]
    if interactive:
        chosen = pick(items, posters)
    else:
        # Piped input: print the list and read numbers instead.
        out = Console()
        for item in items:
            for line in item:
                out.print(line)
        try:
            picked = input("\nPick (e.g. 1 4 7, Enter to quit): ").split()
        except EOFError:
            return
        chosen = [int(p) - 1 for p in picked if p.isdigit() and 1 <= int(p) <= len(entries)]
    picks = [entries[i] for i in chosen]
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
