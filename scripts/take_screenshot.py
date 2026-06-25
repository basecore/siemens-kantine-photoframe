#!/usr/bin/env python3
"""Siemens Kantine Regensburg – weekly menu scraper + 600x800 JPEG renderer.

Approach:
  1. Load page with Playwright  –> page.content() (raw HTML)
  2. Parse menu data from HTML with BeautifulSoup (no JS evaluate)
  3. Render clean 600x800 portrait JPEG with Pillow
  4. Save to docs/images/kantine_YYYY-Www.jpg
"""
import os
import re
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ── Config ─────────────────────────────────────────────────────────────────
URL_MENU = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID = os.environ.get("CATERINGPORTAL_SID", "").strip()
if _SID:
    URL_MENU = f"{URL_MENU}?ste_sid={_SID}"

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8

W, H = 600, 800   # portrait for Philips 8FF3WMI

# ── Colours ─────────────────────────────────────────────────────────────────
BLUE   = (0,  57, 107)
LIGHT  = (0, 119, 193)
R_ODD  = (240, 246, 252)
R_EVEN = (255, 255, 255)
C_VG   = ( 34, 139,  34)
C_V    = (100, 180,  60)
C_TXT  = ( 30,  30,  30)
WHITE  = (255, 255, 255)
GRID   = (200, 215, 230)

# ── Font helper ───────────────────────────────────────────────────────────────
_FREG = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]
_FBOL = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

def lf(size, bold=False):
    for p in (_FBOL if bold else _FREG) + _FREG:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()

# ── Time helpers ─────────────────────────────────────────────────────────────
def german_time(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Berlin"))
    except ImportError:
        pass
    try:
        import pytz
        return dt.astimezone(pytz.timezone("Europe/Berlin"))
    except ImportError:
        pass
    import calendar
    yr = dt.year
    def last_sun(y, m):
        ld = calendar.monthrange(y, m)[1]
        d  = datetime(y, m, ld, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    cs = last_sun(yr, 3).replace(hour=1)
    ce = last_sun(yr, 10).replace(hour=1)
    return dt + timedelta(hours=2 if cs <= dt < ce else 1)

def kw_label(dt):
    d = german_time(dt)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", int(w)

# ── HTML scraper ──────────────────────────────────────────────────────────────
NORM = lambda s: re.sub(r"\s+", " ", s or "").strip()

DAY_PATTERNS = [
    ("Mo", re.compile(r"^(montag|mo\.?)(\s|,|\d|$)", re.I)),
    ("Di", re.compile(r"^(dienstag|di\.?)(\s|,|\d|$)", re.I)),
    ("Mi", re.compile(r"^(mittwoch|mi\.?)(\s|,|\d|$)", re.I)),
    ("Do", re.compile(r"^(donnerstag|do\.?)(\s|,|\d|$)", re.I)),
    ("Fr", re.compile(r"^(freitag|fr\.?)(\s|,|\d|$)", re.I)),
]

CAT_PATTERNS = [
    ("Suppe",   re.compile(r"suppe|vorspeise|soup|cremesuppe", re.I)),
    ("Essen 1", re.compile(r"essen\s*1|men[\u00fc]\s*1|gericht\s*1|hauptgericht\s*1", re.I)),
    ("Essen 2", re.compile(r"essen\s*2|men[\u00fc]\s*2|gericht\s*2|hauptgericht\s*2", re.I)),
    ("Essen 3", re.compile(r"essen\s*3|men[\u00fc]\s*3|gericht\s*3|hauptgericht\s*3|vegetar|vegan", re.I)),
]

PRICE_RE = re.compile(r"(\d+[,.]\d{2})\s*\u20ac?")

def detect_vv(text):
    t = text.lower()
    if "vegan" in t:
        return "VG"
    if "vegetar" in t or re.search(r"\bveg\b", t):
        return "V"
    return ""

def extract_price(text):
    m = PRICE_RE.search(text)
    return (m.group(1).replace(".", ",") + " €") if m else ""

def detect_day(text):
    for short, pat in DAY_PATTERNS:
        if pat.match(text):
            return short
    return None

def detect_cat(text):
    for cat, pat in CAT_PATTERNS:
        if pat.search(text):
            return cat
    return None

# --- Strategy A: look for structured day-containers in HTML ---
def try_strategy_a(soup, local_dt):
    """Find repeated .day / [data-weekday] blocks with child meal items."""
    day_container_selectors = [
        {"class": re.compile(r"day|weekday|tag", re.I)},
        {"data-weekday": True},
        {"data-day": True},
    ]
    candidates = []
    for sel in day_container_selectors:
        blocks = soup.find_all(True, sel)
        if len(blocks) >= 4:
            candidates = blocks
            break
    if not candidates:
        return {}

    monday = local_dt - timedelta(days=local_dt.weekday())
    day_short = ["Mo", "Di", "Mi", "Do", "Fr"]
    dates = {day_short[i]: (monday + timedelta(days=i)).strftime("%d.%m") for i in range(5)}

    week = {}
    for block in candidates:
        hdr = block.find(["h1","h2","h3","h4","strong","b","span","div"], recursive=False)
        day_txt = NORM(hdr.get_text() if hdr else block.get("data-weekday","") or block.get("data-day",""))
        short = detect_day(day_txt)
        if not short:
            continue
        label = f"{short} {dates.get(short,'')}"
        meals = []
        meal_selectors = [
            {"class": re.compile(r"meal|dish|speise|gericht|item", re.I)},
        ]
        items = []
        for ms in meal_selectors:
            items = block.find_all(True, ms)
            if items:
                break
        if not items:
            items = block.find_all("li")
        for idx, item in enumerate(items[:4]):
            txt = NORM(item.get_text(" ", strip=True))
            if len(txt) < 5:
                continue
            cat = detect_cat(txt) or f"Essen {idx+1}" if idx > 0 else "Suppe"
            meals.append({
                "kategorie": cat,
                "name": txt,
                "vv": detect_vv(txt),
                "preis_int": extract_price(txt),
            })
        if meals:
            week[label] = meals
    return week

# --- Strategy B: parse embedded JSON (window.__INITIAL_STATE__ etc.) ---
def try_strategy_b(soup, local_dt):
    monday = local_dt - timedelta(days=local_dt.weekday())
    day_short = ["Mo", "Di", "Mi", "Do", "Fr"]
    dates = {day_short[i]: (monday + timedelta(days=i)).strftime("%d.%m") for i in range(5)}

    json_candidates = []
    for tag in soup.find_all("script"):
        txt = tag.string or ""
        for pattern in [
            r"window\.__(?:INITIAL_STATE|NUXT|DATA|APP)__\s*=\s*(\{.*?\})\s*;",
            r"<script[^>]+type=[\"']application/json[\"'][^>]*>(.*?)</script>",
        ]:
            m = re.search(pattern, txt, re.S)
            if m:
                json_candidates.append(m.group(1))
        if len(txt) > 100 and txt.strip().startswith("{"):
            json_candidates.append(txt.strip())

    for raw in json_candidates:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        week = _walk_json_for_menu(data, dates)
        if week:
            return week
    return {}

def _walk_json_for_menu(obj, dates, depth=0):
    """Recursively search JSON for menu-like structures."""
    if depth > 8:
        return {}
    week = {}
    if isinstance(obj, dict):
        keys = [str(k).lower() for k in obj]
        meal_keys = [k for k in keys if any(x in k for x in ["meal","dish","menu","speise","gericht","item"])]
        if meal_keys and any(k in keys for k in ["name","title","bezeichnung"]):
            return {}
        for v in obj.values():
            sub = _walk_json_for_menu(v, dates, depth+1)
            if sub:
                week.update(sub)
    elif isinstance(obj, list) and len(obj) >= 4:
        # might be a list of days
        day_short = ["Mo", "Di", "Mi", "Do", "Fr"]
        for i, item in enumerate(obj[:5]):
            if isinstance(item, dict):
                name_candidates = [item.get(k,"") for k in ["name","title","day","weekday","dayname","tag"] if item.get(k)]
                day_name = NORM(str(name_candidates[0])) if name_candidates else ""
                short = detect_day(day_name) or (day_short[i] if i < 5 else None)
                if not short:
                    continue
                label = f"{short} {dates.get(short,'')}"
                meals_raw = item.get("meals") or item.get("dishes") or item.get("items") or item.get("speisen") or []
                meals = []
                for midx, m in enumerate(meals_raw[:4]):
                    if isinstance(m, dict):
                        n = NORM(str(m.get("name") or m.get("title") or m.get("bezeichnung") or ""))
                        p = extract_price(str(m.get("price") or m.get("preis") or m.get("priceInt") or ""))
                        if n and len(n) > 4:
                            meals.append({
                                "kategorie": detect_cat(n) or ("Suppe" if midx==0 else f"Essen {midx}"),
                                "name": n,
                                "vv": detect_vv(n),
                                "preis_int": p,
                            })
                if meals:
                    week[label] = meals
    return week

# --- Strategy C: line-by-line text parsing (robust last-resort) ---
def try_strategy_c(soup, local_dt):
    monday = local_dt - timedelta(days=local_dt.weekday())
    day_short = ["Mo", "Di", "Mi", "Do", "Fr"]
    dates = {day_short[i]: (monday + timedelta(days=i)).strftime("%d.%m") for i in range(5)}

    lines = [NORM(x) for x in soup.get_text("\n", strip=True).splitlines() if NORM(x)]

    week = {}
    current_day = None
    current_cat = None
    buffer = []

    def flush():
        if current_day and current_cat and buffer:
            txt = " ".join(buffer).strip()
            if len(txt) > 4:
                label = f"{current_day} {dates.get(current_day,'')}"
                week.setdefault(label, [])
                week[label].append({
                    "kategorie": current_cat,
                    "name":      txt,
                    "vv":        detect_vv(txt),
                    "preis_int": extract_price(txt),
                })

    for line in lines:
        day = detect_day(line)
        if day:
            flush()
            buffer = []
            current_day = day
            current_cat = None
            continue
        cat = detect_cat(line)
        if cat:
            flush()
            buffer = [line]
            current_cat = cat
            continue
        if current_cat and len(line) > 4 and not line.isdigit():
            buffer.append(line)

    flush()
    return week

def scrape_menu(html, local_dt):
    soup = BeautifulSoup(html, "html.parser")
    # Remove scripts / styles from text parsing
    for tag in soup(["script","style","noscript"]):
        tag.decompose()

    for strategy_name, fn in [
        ("A (DOM containers)", try_strategy_a),
        ("B (embedded JSON)", try_strategy_b),
        ("C (line-by-line text)", try_strategy_c),
    ]:
        result = fn(soup, local_dt)
        if result:
            print(f"  Scraping strategy {strategy_name}: {len(result)} days found")
            for day, meals in result.items():
                print(f"    {day}: {len(meals)} meals")
                for m in meals[:2]:
                    print(f"      [{m['kategorie']}] {m['name'][:60]} | vv={m['vv']} | {m['preis_int']}")
            return result
        else:
            print(f"  Scraping strategy {strategy_name}: no data")
    return {}

# ── Image renderer ────────────────────────────────────────────────────────────
def wrap_text(draw, text, f, max_w):
    words = text.split()
    out, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        b = draw.textbbox((0, 0), t, font=f)
        if b[2] - b[0] <= max_w:
            cur = t
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out

ROW_HEIGHTS = {"Suppe": 46, "Essen 1": 64, "Essen 2": 64, "Essen 3": 64}
CATS = ["Suppe", "Essen 1", "Essen 2", "Essen 3"]

def render(week_data, kw, label, local_dt):
    img  = Image.new("RGB", (W, H), (255, 255, 255))
    d    = ImageDraw.Draw(img)
    ftit = lf(13, True)
    fday = lf(10, True)
    fcat = lf( 8, True)
    ftxt = lf( 9, False)
    fbdg = lf( 8, True)
    fprc = lf( 8, False)
    fftr = lf( 9, False)

    # Header
    d.rectangle([(0,0),(W,36)], fill=BLUE)
    title = f"Siemens Kantine Regensburg  |  KW {kw:02d}"
    b = d.textbbox((0,0), title, font=ftit)
    d.text(((W-(b[2]-b[0]))//2, 10), title, font=ftit, fill=WHITE)
    y = 36

    # No data fallback
    if not week_data:
        d.text((20, y+40), "Speiseplan konnte nicht geladen werden.", font=ftxt, fill=C_TXT)
        d.text((20, y+60), "Bitte manuell prüfen:",                  font=ftxt, fill=C_TXT)
        d.text((20, y+80), URL_MENU,                                  font=ftxt, fill=LIGHT)
        _footer(d, kw, label, local_dt, fftr)
        return img

    days = list(week_data.keys())[:5]
    dw   = W // max(1, len(days))

    # Day-header row
    for i, day in enumerate(days):
        x = i * dw
        d.rectangle([(x, y),(x+dw-1, y+21)], fill=LIGHT)
        b = d.textbbox((0,0), day, font=fday)
        d.text((x + (dw-(b[2]-b[0]))//2, y+5), day, font=fday, fill=WHITE)
        if i > 0:
            d.line([(x,y),(x,y+21)], fill=BLUE, width=1)
    y += 22

    # Meal rows
    for ri, cat in enumerate(CATS):
        rh = ROW_HEIGHTS[cat]
        d.rectangle([(0,y),(W,y+rh-1)], fill=R_ODD if ri%2==0 else R_EVEN)
        d.line([(0,y),(W,y)], fill=GRID, width=1)

        for i, day in enumerate(days):
            x = i * dw
            if i > 0:
                d.line([(x,y),(x,y+rh)], fill=GRID, width=1)

            items = [it for it in week_data.get(day, []) if it["kategorie"] == cat]
            if not items:
                b = d.textbbox((0,0), "–", font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2, y+rh//2-6), "–", font=ftxt, fill=(180,180,180))
                continue

            it = items[0]
            cx, cy = x+4, y+4
            avw = dw - 8

            # Category label (small)
            d.text((cx, cy), it["kategorie"], font=fcat, fill=(100,100,100))
            cy += 11

            # Vegan / Veg badge
            if it["vv"]:
                bl = "Vegan" if it["vv"]=="VG" else "Veg."
                bc = C_VG   if it["vv"]=="VG" else C_V
                b  = d.textbbox((0,0), bl, font=fbdg)
                bw = b[2]-b[0]+6; bh = b[3]-b[1]+3
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh)], radius=3, fill=bc)
                d.text((cx+3,cy+1), bl, font=fbdg, fill=WHITE)
                cy += bh + 3

            # Dish name (word-wrapped, max 3 lines)
            max_lines = 2 if cat=="Suppe" else 3
            for ln in wrap_text(d, it["name"], ftxt, avw)[:max_lines]:
                d.text((cx,cy), ln, font=ftxt, fill=C_TXT)
                cy += 12

            # Price – bottom right
            if it["preis_int"]:
                pl = f"Int: {it['preis_int']}"
                b  = d.textbbox((0,0), pl, font=fprc)
                d.text((x+dw-(b[2]-b[0])-4, y+rh-(b[3]-b[1])-4),
                       pl, font=fprc, fill=LIGHT)
        y += rh

    # Grid bottom line
    d.line([(0,y),(W,y)], fill=GRID, width=1)
    y += 1

    # Legend
    if y + 20 < H - 25:
        d.rectangle([(0,y),(W,y+18)], fill=(245,249,253))
        d.rectangle([( 6,y+4),(18,y+14)], fill=C_VG)
        d.text((22,y+4), "Vegan",       font=fprc, fill=C_TXT)
        d.rectangle([(68,y+4),(80,y+14)], fill=C_V)
        d.text((84,y+4), "Vegetarisch",  font=fprc, fill=C_TXT)
        d.text((170,y+4), "Int = Mitarbeiterpreis", font=fprc, fill=(120,120,120))

    _footer(d, kw, label, local_dt, fftr)
    return img

def _footer(d, kw, label, local_dt, f):
    txt = (f"KW {kw:02d} / {label}  –  "
           f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  –  siemens.cateringportal.io")
    d.rectangle([(0,H-24),(W,H)], fill=BLUE)
    b = d.textbbox((0,0), txt, font=f)
    d.text(((W-(b[2]-b[0]))//2, H-17), txt, font=f, fill=WHITE)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now      = datetime.now(timezone.utc)
    local    = german_time(now)
    label, kw = kw_label(now)
    out_path = OUT_DIR / f"kantine_{label}.jpg"

    print(f"Target URL : {URL_MENU}")
    print(f"Week label : {label}  (KW {kw:02d})")

    # 1. Load page HTML
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page    = browser.new_page(viewport={"width": 1200, "height": 900})

        print("Loading page...")
        page.goto(URL_MENU, wait_until="load", timeout=60000)
        page.wait_for_timeout(3000)   # let SPA render
        print(f"Title: {page.title()}")

        html = page.content()
        browser.close()

    print(f"HTML size: {len(html):,} bytes")

    # 2. Scrape menu from HTML
    week_data = scrape_menu(html, local)
    if not week_data:
        print("WARNING: No menu data found in HTML – rendering placeholder image.")

    # 3. Render JPEG
    img = render(week_data, kw, label, local)
    img.save(str(out_path), "JPEG", quality=92)
    print(f"Saved: {out_path}  ({img.size[0]}x{img.size[1]})")

    # 4. Cleanup (keep last MAX_KEEP weeks)
    for old in sorted(OUT_DIR.glob("kantine_*.jpg"))[:-MAX_KEEP]:
        old.unlink()
        print(f"Removed: {old}")


if __name__ == "__main__":
    main()
