# yt-summary

Fetches a YouTube video's transcript and summarises it with the Claude API, in Italian when the chosen transcript is Italian and in English otherwise.

## Setup

- [uv](https://docs.astral.sh/uv/) (`brew install uv`). Dependencies install on first run.
- `ANTHROPIC_API_KEY` set in your shell environment. Never commit it.

## Usage

    ./yt_summary.py "https://youtu.be/VIDEO_ID"

Prints the summary and copies it to the clipboard (macOS).

## Notes

- Human captions are preferred, auto-generated ones used as fallback. Within each, English comes first, then Italian.
- Auto-generated tracks can include machine dubs, so an Italian video with only auto captions is summarised in English.
- YouTube blocks transcript requests from cloud and VPN IPs. Run it from a residential connection.
