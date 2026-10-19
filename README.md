# Hinge auto-liker

Runs on your computer and controls your Android phone over USB. For each Discover
profile it scrolls through the whole profile, takes screenshots, and asks Claude to
like or skip. On a like, Claude writes a first comment tied to one of her prompts or
photos, and the bot sends the like with that comment. It never opens the Matches tab
or replies to conversations.

## Setup (once)

1. **Phone:** turn on Developer options (tap Build number 7 times), then turn on
   **USB debugging**. Plug the phone into your computer and accept the prompt.
2. **Computer:** install platform-tools (`brew install android-platform-tools`, or
   download from developer.android.com). `adb devices` should list your phone.
3. Install dependencies and the helper app:
   ```
   pip install -r requirements.txt
   python -m uiautomator2 init
   ```
4. `export ANTHROPIC_API_KEY=...`, or run `ant auth login`.
5. In Hinge, set your preferences (age 22–28, ethnicity East Asian) and turn them on
   as dealbreakers.
6. Edit `config.yaml`, especially `about_me`. Better input about you means better openers.

## Run

Open Hinge on the Discover tab, then:

```
python hinge_bot.py run
```

With `review_mode: true`, you approve, edit or skip each opener in the terminal.
Every decision is logged to `likes_log.csv`.

The bot stops when it reaches the daily cap, when Hinge says you're out of profiles
or likes, or when it hits an unexpected screen three times in a row.

## If a tap misses

Hinge's layout changes between versions. The bot tries the UI selectors in
`config.yaml` first and falls back to finding buttons in screenshots. If something
keeps missing, open that screen and run `python hinge_bot.py inspect`. Then fix the
selectors using the XML it saves, or send the XML to Claude.

## Cost

Each profile takes one call to read and decide, plus 1–3 small calls to find buttons.
On Opus that's roughly $0.03–0.08 per profile, depending on screenshot count. To cut
cost, set `model: claude-sonnet-5-5` in the config.

## Risk

Automation is against Hinge's terms. Accounts flagged for bot behavior can be banned,
and bans often follow your phone number. The daily cap and random delays lower the
risk but can't remove it.
