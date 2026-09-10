"""Stream Deck tile rendering; no runtime or control commands."""
import colorsys
import functools
import hashlib
from PIL import Image, ImageDraw, ImageFont
from StreamDeck.ImageHelpers import PILHelper

SHOW_ICONS = True
_ICON_CACHE = {}
FOREVER = float("inf")
FONT_PATHS = ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/Supplemental/Arial.ttf"]
STATUS = {
    "blocked": (0, (215, 140, 20), "NEEDS YOU"),
    "working": (1, (30, 90, 190), "working"),
    "done": (2, (35, 130, 70), "done"),
    "idle": (3, (210, 214, 222), "idle"),
    "unknown": (4, (90, 90, 90), "unknown"),
}
DEFAULT = STATUS["unknown"]
NEEDS_HUMAN = {"blocked"}

def group_color(group_id):
    """Stable accent color for a named session."""
    if not group_id:
        return (90, 90, 90)
    hue = int(hashlib.md5(group_id.encode()).hexdigest(), 16) % 360 / 360
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.95)
    return (round(r * 255), round(g * 255), round(b * 255))


def _identicon(name, size):
    """Deterministic GitHub-style identicon from a repo name: a 5x5, vertically
    mirrored block pattern colored by the name hash. Transparent background so the
    tile's state color shows between blocks."""
    h = hashlib.md5((name or "?").encode()).digest()
    r, g, b = colorsys.hsv_to_rgb(h[-1] / 255, 0.6, 0.9)
    fg = (round(r * 255), round(g * 255), round(b * 255), 255)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pad = size * 0.12
    cell = (size - 2 * pad) / 5
    for i in range(15):                # 3 cols x 5 rows, mirrored into 5 cols
        if h[i] & 1:
            col, row = i // 5, i % 5
            for c in (col, 4 - col):
                x, y = pad + c * cell, pad + row * cell
                d.rectangle([x, y, x + cell, y + cell], fill=fg)
    return im


def icon_image(item, size):
    """Identicon cached by session and workspace label."""
    name = f"{item.get('machine_id', 'local')}:{item.get('session') or 'default'}:{item.get('label', '?')}"
    if name not in _ICON_CACHE:
        _ICON_CACHE[name] = _identicon(name, 128)
    return _ICON_CACHE[name].resize((size, size))


def age_label(ms, now_ms):
    """Compact time since an observed state change: 45s / 12m / 4h / 2d. Blank
    before the first observation."""
    if not ms:
        return ""
    secs = max(0, int(now_ms) - int(ms)) // 1000    # now_ms is a float clock
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def text_color(bg):
    """Black on light tiles, white on dark — so 'idle' white tiles stay legible."""
    r, g, b = bg
    return (20, 20, 20) if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else (255, 255, 255)


@functools.lru_cache(maxsize=16)
def load_font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_tile(deck, item, now_ms=0, wash=None):
    img = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    if item is None:
        if wash:    # empty keys glow with the fleet's worst state, so the unit
            draw.rectangle([0, 0, w, h],     # reads from across the room
                           fill=tuple(round(c * 0.35) for c in wash))
        return PILHelper.to_native_format(deck, img)
    _, color, label = STATUS.get(item["state"], DEFAULT)
    fg = text_color(color)
    draw.rectangle([0, 0, w, h], fill=color)
    if SHOW_ICONS:
        # state color stays the frame/background; icon centered, name below.
        show_machine = item.get("show_machine")
        icon = icon_image(item, round(min(w, h) * (0.32 if show_machine else 0.42)))
        img.paste(icon, ((w - icon.width) // 2, round(h * 0.13)), icon)
        if show_machine:
            draw.text((w // 2, h * 0.53), item["machine_label"][:14],
                      font=load_font(round(w * 0.105)), anchor="mm", fill=fg)
        draw.text((w // 2, h * 0.68), item["label"][:12],
                  font=load_font(round(w * 0.13)), anchor="mm", fill=fg)
        draw.text((w // 2, h * 0.86), item["sub"][:14],
                  font=load_font(round(w * 0.115)), anchor="mm", fill=fg)
    else:
        sub_fg = tuple(int(c * 0.55 + f * 0.45) for c, f in zip(color, fg))  # muted
        draw.text((w // 2, h // 2 - h * 0.15), item["label"][:11],
                  font=load_font(round(w * 0.19)), anchor="mm", fill=fg)
        draw.text((w // 2, h // 2 + h * 0.07), (item["sub"] or "")[:14],
                  font=load_font(round(w * 0.13)), anchor="mm", fill=sub_fg)
        draw.text((w // 2, h - h * 0.13), label, font=load_font(round(w * 0.13)),
                  anchor="mm", fill=fg)
    # Corner marks, each short enough to stay legible on a Mini key: age top-right,
    # agent kind top-left, and an unread dot bottom-right.
    corner = load_font(round(w * 0.12))
    age = age_label(item.get("state_since"), now_ms)
    if age:
        draw.text((w - w * 0.04, h * 0.04), age, anchor="ra", font=corner, fill=fg)
    draw.text((w * 0.04, h * 0.04), item["agent_type"][:6], anchor="la", font=corner, fill=fg)
    if item.get("unread"):
        r = max(2, w * 0.045)
        draw.ellipse([w - w * 0.05 - 2 * r, h - h * 0.05 - 2 * r,
                      w - w * 0.05, h - h * 0.05], fill=fg)
    if item.get("machine_id", "local") != "local" or item.get("session") != "default":
        group = f"{item.get('machine_id', 'local')}:{item['session']}"
        draw.rectangle([w - max(3, w * 0.04), 0, w, h], fill=group_color(group))
    return PILHelper.to_native_format(deck, img)


def render_action(deck, label, sub="", color=(60, 60, 66), enabled=True):
    """One key on the agent page: a verb, and what it will do right now."""
    img = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    if not enabled:
        color = tuple(round(c * 0.3) for c in color)
    draw.rectangle([0, 0, w, h], fill=color)
    fg = text_color(color) if enabled else (120, 120, 126)
    draw.text((w // 2, h // 2 - (h * 0.12 if sub else 0)), label,
              font=load_font(round(w * 0.19)), anchor="mm", fill=fg)
    if sub:
        draw.text((w // 2, h // 2 + h * 0.18), sub[:12],
                  font=load_font(round(w * 0.12)), anchor="mm", fill=fg)
    return PILHelper.to_native_format(deck, img)


def auto_badge(until, now):
    """Status-key badge for the auto-approver: how much longer it stays armed.
    Blank when off. Time remaining beats the word "on" — the whole point of the
    window is knowing it's closing."""
    if not until:
        return ""
    if until == FOREVER:
        return "AUTO ON"
    return "AUTO " + age_label(now * 1000, until * 1000)   # elapsed helper, run backwards


def render_status(deck, count, page, pages, down=False, auto="", machines=()):
    img = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    if down:
        draw.rectangle([0, 0, w, h], fill=(200, 40, 40))
        draw.text((w // 2, h // 2), "HERDR\nDOWN", font=load_font(round(w * 0.16)),
                  anchor="mm", align="center", fill=(255, 255, 255))
        return PILHelper.to_native_format(deck, img)
    offline = [machine for machine in machines if machine["state"] == "offline"]
    connecting = any(machine["state"] == "connecting" for machine in machines)
    online = sum(machine["state"] == "online" for machine in machines)
    draw.rectangle([0, 0, w, h], fill=(200, 40, 40) if count else (40, 42, 50))
    if count:
        draw.text((w // 2, h // 2 - h * 0.12), str(count),
                  font=load_font(round(w * 0.4)), anchor="mm", fill=(255, 255, 255))
        draw.text((w // 2, h * 0.73), "NEED YOU", font=load_font(round(w * 0.13)),
                  anchor="mm", fill=(255, 255, 255))
    elif offline:
        label = offline[0]["label"][:14] if len(offline) == 1 else f"{len(offline)} machines"
        draw.text((w // 2, h * 0.42), label, font=load_font(round(w * 0.12)),
                  anchor="mm", fill=(240, 170, 40))
        draw.text((w // 2, h * 0.64), "OFFLINE", font=load_font(round(w * 0.14)),
                  anchor="mm", fill=(240, 170, 40))
    else:
        draw.text((w // 2, h // 2), "connecting" if connecting else "all clear", font=load_font(round(w * 0.15)),
                  anchor="mm", fill=(150, 155, 165))
    if machines:
        draw.text((w // 2, h * 0.93), f"{online}/{len(machines)} ONLINE",
                  font=load_font(max(8, round(w * 0.11))), anchor="mm",
                  fill=(240, 170, 40) if offline else (180, 190, 200))
    if pages > 1:
        draw.text((w - w * 0.02, h * 0.06), f"{page + 1}/{pages}",
                  font=load_font(round(w * 0.12)), anchor="ra", fill=(230, 230, 235))
    if auto:  # codex auto-approve is armed — amber, the same "hands off" hue
        draw.text((w * 0.04, h * 0.06), auto, font=load_font(round(w * 0.12)),
                  anchor="la", fill=(240, 170, 40))
    return PILHelper.to_native_format(deck, img)
