# yt-summary

Fetches a YouTube video's transcript and summarises it with the Claude API, in Italian when the chosen transcript is Italian and in English otherwise.

## Setup

- [uv](https://docs.astral.sh/uv/) (`brew install uv`). Dependencies install on first run.
- `ANTHROPIC_API_KEY` set in your shell environment. Never commit it.

## Usage

    ./yt_summary.py "https://youtu.be/VIDEO_ID"

Prints the summary. Summary and transcript are saved to `transcripts/<id>.md` next to the script (gitignored). If that file already exists, the saved summary is reused with no transcript fetch or API call. Delete the file to summarise again.

It also copies a follow-up prompt to the clipboard (macOS): a short instruction plus the summary and full transcript. Paste it into Claude, ChatGPT or any other chat to ask questions about the video.

## Notes

- Human captions are preferred, auto-generated ones used as fallback. Within each, English comes first, then Italian.
- Auto-generated tracks can include machine dubs, so an Italian video with only auto captions is summarised in English.
- YouTube blocks transcript requests from cloud and VPN IPs. Run it from a residential connection.
