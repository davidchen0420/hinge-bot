"""Hinge auto-liker: reads each Discover profile, decides like/skip, and sends a
like with a personalized first comment. Never opens Matches or replies to chats.

Usage:
    python hinge_bot.py run        # main loop
    python hinge_bot.py inspect    # dump current screen's UI tree + screenshot
"""

import base64
import csv
import datetime as dt
import hashlib
import io
import random
import sys
import time
from pathlib import Path
from typing import Literal, Optional

import anthropic
import uiautomator2 as u2
import yaml
from PIL import Image
from pydantic import BaseModel

HERE = Path(__file__).parent
CONFIG = yaml.safe_load((HERE / "config.yaml").read_text())
LOG_PATH = HERE / "likes_log.csv"
HINGE_PKG = "co.hinge.app"
MODEL = CONFIG.get("model", "claude-opus-5-5")

client = anthropic.Anthropic()


# ---------- model I/O ----------

class ProfileDecision(BaseModel):
    screen_state: Literal["profile", "out_of_likes", "no_more_profiles", "match_popup", "other"]
    name: str
    age: Optional[int]
    listed_ethnicity: Optional[str]
    decision: Literal["like", "skip"]
    reason: str
    target_screenshot: int  # index of the screenshot holding the photo/prompt to comment on
    target_description: str  # e.g. 'prompt "My simple pleasures" card' or 'second photo (hiking)'
    opener: str


class Location(BaseModel):
    found: bool
    x: int  # 0-1000, fraction of image width
    y: int  # 0-1000, fraction of image height


def _img_block(img: Image.Image) -> dict:
    img = img.convert("RGB")
    img.thumbnail((720, 1600))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    data = base64.standard_b64encode(buf.getvalue()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


DECIDE_PROMPT = """You are helping me (the account owner) use Hinge. The screenshots below are one \
Discover profile, top to bottom (index 0 = top).

My preferences:
{criteria}

About me (for writing the opener):
{about_me}

Tasks:
1. screen_state: is this a normal Discover profile, an out-of-likes screen, a "no more profiles" \
screen, a match popup, or something else?
2. Read her name, age, and listed ethnicity (only if she lists it as text on the profile; never \
guess from photos; null if not listed).
3. decision: like or skip, based on my preferences. Give a short reason.
4. If like: pick the ONE photo or prompt that gives the best conversation hook. Return the \
screenshot index it appears in and a short description identifying it.
5. opener: a first comment attached to that like. Rules: under 25 words, specific to what she \
wrote or showed, casual and warm, playful is good, end with a question or easy hook. No pickup \
lines, no comments on her body, no generic "hey", no emojis unless natural. Sound like a real \
person texting, not a copywriter.
If skipping, return an empty opener and target_screenshot 0."""


def decide(shots: list[Image.Image]) -> Optional[ProfileDecision]:
    content = []
    for i, s in enumerate(shots):
        content.append({"type": "text", "text": f"Screenshot {i}:"})
        content.append(_img_block(s))
    content.append({"type": "text", "text": DECIDE_PROMPT.format(
        criteria=CONFIG["criteria"], about_me=CONFIG["about_me"])})
    resp = client.messages.parse(
        model=MODEL, max_tokens=16000,
        messages=[{"role": "user", "content": content}],
        output_format=ProfileDecision,
    )
    if resp.stop_reason == "refusal":
        return None
    return resp.parsed_output


def locate(shot: Image.Image, what: str) -> Optional[tuple[float, float]]:
    resp = client.messages.parse(
        model=MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": [
            _img_block(shot),
            {"type": "text", "text": f"This is a Hinge screen. Find: {what}. Return its center as "
                                     "x,y in 0-1000 coordinates of the image (found=false if not visible)."},
        ]}],
        output_format=Location,
    )
    loc = resp.parsed_output
    if resp.stop_reason == "refusal" or not loc or not loc.found:
        return None
    return loc.x / 1000, loc.y / 1000


# ---------- device actions ----------

class Phone:
    def __init__(self):
        self.d = u2.connect(CONFIG.get("device_serial") or None)
        self.w, self.h = self.d.window_size()

    def pause(self, lo=0.8, hi=2.0):
        time.sleep(random.uniform(lo, hi))

    def shot(self) -> Image.Image:
        return self.d.screenshot()

    def tap_frac(self, fx: float, fy: float):
        self.d.click(int(fx * self.w), int(fy * self.h))

    def scroll_down(self):
        self.d.swipe(self.w * 0.5, self.h * 0.75, self.w * 0.5, self.h * 0.30, 0.35)
        self.pause(0.8, 1.4)

    def scroll_to_top(self, n: int):
        for _ in range(n + 2):
            self.d.swipe(self.w * 0.5, self.h * 0.30, self.w * 0.5, self.h * 0.85, 0.2)
            time.sleep(0.4)
        self.pause()

    def ensure_hinge(self):
        if self.d.app_current().get("package") != HINGE_PKG:
            self.d.app_start(HINGE_PKG)
            time.sleep(6)

    def capture_profile(self, max_shots=8) -> list[Image.Image]:
        shots, last = [], None
        for _ in range(max_shots):
            s = self.shot()
            digest = hashlib.md5(s.resize((64, 128)).tobytes()).hexdigest()
            if digest == last:  # reached the bottom
                break
            shots.append(s)
            last = digest
            self.scroll_down()
        return shots

    def click_selector(self, sel: dict) -> bool:
        el = self.d(**sel)
        if el.exists(timeout=2):
            el.click()
            return True
        return False

    def tap_or_locate(self, selectors: list[dict], description: str) -> bool:
        for sel in selectors:
            if self.click_selector(sel):
                return True
        pos = locate(self.shot(), description)
        if pos:
            self.tap_frac(*pos)
            return True
        return False


def skip_profile(p: Phone):
    p.scroll_to_top(8)
    ok = p.tap_or_locate(CONFIG["selectors"]["skip"], "the round X / skip button")
    if not ok:
        print("  ! couldn't find skip button")
    p.pause(1.5, 3)


def like_with_comment(p: Phone, n_shots: int, dec: ProfileDecision) -> bool:
    # Return to the screenshot that holds the target element.
    p.scroll_to_top(n_shots)
    for _ in range(dec.target_screenshot):
        p.scroll_down()

    heart = None
    for nudge in (0, 1, -1):  # scroll inertia varies; check neighbours too
        if nudge == 1:
            p.scroll_down()
        elif nudge == -1:
            p.d.swipe(p.w * 0.5, p.h * 0.35, p.w * 0.5, p.h * 0.80, 0.35)
            p.pause()
        heart = locate(p.shot(), f"the heart/like button belonging to the {dec.target_description}")
        if heart:
            break
    if not heart:
        print("  ! couldn't locate heart button for target")
        return False
    p.tap_frac(*heart)
    p.pause(1.5, 2.5)

    box = p.d(className="android.widget.EditText")
    if not box.exists(timeout=4):
        print("  ! comment box didn't open")
        p.d.press("back")
        return False
    box.set_text(dec.opener)
    p.pause(1.0, 2.0)
    try:
        p.d.hide_keyboard()
    except Exception:
        pass

    if not p.tap_or_locate(CONFIG["selectors"]["send_like"], "the 'Send like' button"):
        print("  ! couldn't find Send like button")
        p.d.press("back")
        return False
    p.pause(2, 3.5)
    return True


# ---------- bookkeeping ----------

def likes_sent_today() -> int:
    if not LOG_PATH.exists():
        return 0
    today = dt.date.today().isoformat()
    with LOG_PATH.open() as f:
        return sum(1 for r in csv.DictReader(f) if r["time"].startswith(today) and r["sent"] == "yes")


def log(dec: ProfileDecision, sent: bool):
    new = not LOG_PATH.exists()
    with LOG_PATH.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "name", "age", "ethnicity", "decision", "reason", "target", "opener", "sent"])
        w.writerow([dt.datetime.now().isoformat(timespec="seconds"), dec.name, dec.age,
                    dec.listed_ethnicity, dec.decision, dec.reason, dec.target_description,
                    dec.opener, "yes" if sent else "no"])


def review(dec: ProfileDecision) -> Optional[str]:
    """Returns the opener to send, or None to skip."""
    print(f"\n  {dec.name}, {dec.age} | {dec.reason}")
    print(f"  target: {dec.target_description}")
    print(f"  opener: {dec.opener}")
    ans = input("  [enter]=send  e=edit  s=skip  q=quit > ").strip().lower()
    if ans == "q":
        sys.exit(0)
    if ans == "s":
        return None
    if ans == "e":
        return input("  your opener > ").strip() or None
    return dec.opener


# ---------- main loop ----------

def run():
    p = Phone()
    p.ensure_hinge()
    cap = CONFIG["daily_like_cap"]
    lo, hi = CONFIG["delay_between_profiles_sec"]
    strikes = 0

    while True:
        sent_today = likes_sent_today()
        if sent_today >= cap:
            print(f"Daily cap reached ({sent_today}/{cap}). Stopping.")
            return
        p.ensure_hinge()
        shots = p.capture_profile()
        dec = decide(shots)
        if dec is None:
            print("  model declined this profile; skipping")
            skip_profile(p)
            continue

        if dec.screen_state != "profile":
            print(f"Screen state: {dec.screen_state}")
            if dec.screen_state in ("out_of_likes", "no_more_profiles"):
                return
            if dec.screen_state == "match_popup":
                p.d.press("back")
                p.pause(2, 3)
                continue
            strikes += 1
            if strikes >= 3:
                print("Unexpected screen 3x in a row; stopping so you can check the phone.")
                return
            p.d.press("back")
            p.pause(2, 4)
            continue
        strikes = 0

        print(f"[{sent_today}/{cap}] {dec.name}, {dec.age}: {dec.decision} ({dec.reason})")
        if dec.decision == "skip":
            log(dec, False)
            skip_profile(p)
        else:
            opener = review(dec) if CONFIG["review_mode"] else dec.opener
            if opener is None:
                log(dec, False)
                skip_profile(p)
            else:
                dec.opener = opener
                sent = like_with_comment(p, len(shots), dec)
                log(dec, sent)
                if not sent:
                    skip_profile(p)

        time.sleep(random.uniform(lo, hi))


def inspect():
    p = Phone()
    out = HERE / "inspect"
    out.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%H%M%S")
    (out / f"ui_{stamp}.xml").write_text(p.d.dump_hierarchy())
    p.shot().save(out / f"screen_{stamp}.png")
    print(f"Saved inspect/ui_{stamp}.xml and inspect/screen_{stamp}.png")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": run, "inspect": inspect}[cmd]()
