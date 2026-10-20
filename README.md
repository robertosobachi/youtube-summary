# yt-summary

Fetches a YouTube video's transcript and summarises it with the Claude API, in Italian when the chosen transcript is Italian and in English otherwise.

## Setup

- [uv](https://docs.astral.sh/uv/) (`brew install uv`). Dependencies install on first run.
- `ANTHROPIC_API_KEY` set in your shell environment. Never commit it.

## Usage

    ./yt_summary.py "https://youtu.be/VIDEO_ID"

Prints the summary. Summary and transcript are saved to `transcripts/<id>.md` next to the script (gitignored). If that file already exists, the saved summary is reused with no transcript fetch or API call. Delete the file to summarise again.

It also copies a follow-up prompt to the clipboard (macOS): a short instruction plus the summary and full transcript. Paste it into Claude, ChatGPT or any other chat to ask questions about the video.

## Sample output

Invented example. In a terminal the Markdown is rendered with colour.

    $ ./yt_summary.py "https://youtu.be/AbCdEfGhIjK"
    Saved to ~/yt-summary/transcripts/AbCdEfGhIjK.md
    # 🍞 Why Your Sourdough Starter Dies in Summer

    ## Summary
    A home baker in a humid coastal city explains why starters that thrive in winter collapse in July. The cause is heat and over-fermentation, not contamination, and the fix is a slower, colder routine.

    ## Main points
    - Above 28°C a 1:1:1 feed is exhausted in under four hours.
    - A sharp, acetone smell means the starter is hungry, not spoiled.
    - Feeding at 1:5:5 stretches the cycle to about twelve hours.
    - A stiff starter at 50% hydration ferments more slowly than a liquid one.
    - Keeping it in the fridge between bakes cuts feeding to once a week.

    ## Key takeaway
    In hot weather, feed at a higher ratio and less often, not more often.

    ────────────────────────────────────────
    Follow-up prompt (summary and transcript) copied to the clipboard.

## Notes

- Human captions are preferred, auto-generated ones used as fallback. Within each, English comes first, then Italian.
- Auto-generated tracks can include machine dubs, so an Italian video with only auto captions is summarised in English.
- YouTube blocks transcript requests from cloud and VPN IPs. Run it from a residential connection.
