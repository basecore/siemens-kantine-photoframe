#!/usr/bin/env python3
"""Siemens Kantine Regensburg – weekly menu PDF scraper + 600x800 JPEG renderer.

Approach:
  1. Load cateringportal.io with Playwright  –> find qnips PDF link in HTML
  2. Download the weekly PDF with requests
  3. Parse menu text from PDF with pdfplumber
  4. Render clean 600x800 portrait JPEG with Pillow
  5. Save to docs/images/kantine_YYYY-Www.jpg
"""
import os
import re
import io
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

try:
    import pdfplumber
except ImportError:
    pdfplumber = None
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

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

# ── Step 1: Find PDF URL from cateringportal ────────────────────────────────────
def find_pdf_url(html: str, kw: int) -> str | None:
    """Search the page HTML for a qnips PDF link matching the current week."""
    # Pattern 1: direct href to qnips PDF
    patterns = [
        r'(https://files\.qnips\.com/[^"\s\']+\.pdf[^"\s\']*)',
        r'(https?://[^"\s\']*qnips[^"\s\']*\.pdf[^"\s\']*)',
        r'href=["\']([^"\s\']*Mittagessen[^"\s\']*\.pdf[^"\s\']*)["\']',
        r'href=["\']([^"\s\']*\.pdf[^"\s\']*)["\']',
    ]
    for pat in patterns:
        matches = re.findall(pat, html, re.I)
        for url in matches:
            if url.startswith("/"):
                url = "https://siemens.cateringportal.io" + url
            # Prefer URL containing current KW number
            if f"_{kw}_" in url or f"_DE_{kw}_" in url or f"W{kw:02d}" in url:
                print(f"  PDF found (KW match): {url[:100]}")
                return url
        # If no KW match, return first PDF found
        for url in matches:
            if url.startswith("/"):
                url = "https://siemens.cateringportal.io" + url
            print(f"  PDF found (first match): {url[:100]}")
            return url
    return None

# ── Step 2: Download PDF ─────────────────────────────────────────────────────────
def download_pdf(url: str) -> bytes:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; KantinoBot/1.0)",
        "Accept": "application/pdf,*/*",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    print(f"  PDF downloaded: {len(r.content):,} bytes")
    return r.content

# ── Step 3: Parse PDF text ──────────────────────────────────────────────────────
def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract all text from PDF, trying pdfplumber first then pypdf."""
    if pdfplumber:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        text = "\n".join(pages)
        if text.strip():
            print(f"  pdfplumber: {len(text)} chars extracted")
            return text
    if PdfReader:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [p.extract_text() or "" for p in reader.pages]
        text = "\n".join(pages)
        print(f"  pypdf: {len(text)} chars extracted")
        return text
    raise RuntimeError("No PDF parser available (pdfplumber / pypdf)")

# ── Step 4: Parse menu from PDF text ─────────────────────────────────────────
NORM = lambda s: re.sub(r"\s+", " ", s or "").strip()

DAY_RE = re.compile(
    r"^(montag|dienstag|mittwoch|donnerstag|freitag|"
    r"mo\.?|di\.?|mi\.?|do\.?|fr\.?)(\s|,|\.|\d|$)",
    re.I,
)
DAY_MAP = {
    "mo": "Mo", "mon": "Mo", "montag": "Mo",
    "di": "Di", "die": "Di", "dienstag": "Di",
    "mi": "Mi", "mit": "Mi", "mittwoch": "Mi",
    "do": "Do", "don": "Do", "donnerstag": "Do",
    "fr": "Fr", "fre": "Fr", "freitag": "Fr",
}
CAT_RE = [
    ("Suppe",   re.compile(r"suppe|vorspeise|cremesuppe|tagessuppe", re.I)),
    ("Essen 1", re.compile(r"essen\s*1|men.\s*1|gericht\s*1|men.\s*i\b",  re.I)),
    ("Essen 2", re.compile(r"essen\s*2|men.\s*2|gericht\s*2|men.\s*ii\b", re.I)),
    ("Essen 3", re.compile(r"essen\s*3|men.\s*3|gericht\s*3|men.\s*iii\b|veg", re.I)),
]
PRICE_RE  = re.compile(r"(\d+[,.]\d{2})\s*\u20ac?")
DATE_RE   = re.compile(r"\d{1,2}\.\d{1,2}\.?(\d{2,4})?")  # 23.06. or 23.06.2026

def detect_day_short(line: str) -> str | None:
    m = DAY_RE.match(line.strip())
    if not m:
        return None
    key = m.group(1).lower().rstrip(".")
    return DAY_MAP.get(key)

def detect_cat(line: str) -> str | None:
    for cat, pat in CAT_RE:
        if pat.search(line):
            return cat
    return None

def detect_vv(text: str) -> str:
    t = text.lower()
    if "vegan" in t:
        return "VG"
    if "vegetar" in t or re.search(r"\bveg\b", t):
        return "V"
    return ""

def extract_price(text: str) -> str:
    m = PRICE_RE.search(text)
    return (m.group(1).replace(".", ",") + " €") if m else ""

def parse_menu_from_text(text: str, local_dt: datetime) -> dict:
    """Parse week menu from raw PDF text into week_data dict."""
    monday = local_dt - timedelta(days=local_dt.weekday())
    dates = {
        "Mo": (monday + timedelta(days=0)).strftime("%d.%m"),
        "Di": (monday + timedelta(days=1)).strftime("%d.%m"),
        "Mi": (monday + timedelta(days=2)).strftime("%d.%m"),
        "Do": (monday + timedelta(days=3)).strftime("%d.%m"),
        "Fr": (monday + timedelta(days=4)).strftime("%d.%m"),
    }

    lines = [NORM(l) for l in text.splitlines() if NORM(l)]

    week: dict = {}
    cur_day: str | None  = None
    cur_cat: str | None  = None
    buf: list            = []

    def flush():
        nonlocal cur_cat, buf
        if cur_day and cur_cat and buf:
            txt = " ".join(buf)
            label = f"{cur_day} {dates.get(cur_day, '')}"
            week.setdefault(label, [])
            # avoid duplicates
            if not any(e["kategorie"] == cur_cat for e in week[label]):
                week[label].append({
                    "kategorie": cur_cat,
                    "name":      txt,
                    "vv":        detect_vv(txt),
                    "preis_int": extract_price(txt),
                })
        buf = []
        cur_cat = None

    for line in lines:
        # skip pure price / date / header lines
        if re.match(r"^[\d,.\u20ac\s]+$", line):
            if cur_cat:
                buf.append(line)
            continue

        day = detect_day_short(line)
        if day:
            flush()
            cur_day = day
            cur_cat = None
            # day line itself may contain a date – skip rest of line
            continue

        cat = detect_cat(line)
        if cat:
            flush()
            cur_cat = cat
            # category label itself is not a dish name
            rest = re.sub(
                r"suppe|vorspeise|essen\s*\d+|men.\s*\d+|gericht\s*\d+",
                "", line, flags=re.I
            ).strip()
            if len(rest) > 4:
                buf = [rest]
            else:
                buf = []
            continue

        if cur_cat and len(line) > 3:
            buf.append(line)

    flush()

    # –– Deduplicate / limit to 4 entries per day ––
    for label in list(week.keys()):
        week[label] = week[label][:4]

    return week

# ── Step 5: Image renderer ─────────────────────────────────────────────────────────
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
        d.rectangle([(x,y),(x+dw-1,y+21)], fill=LIGHT)
        b = d.textbbox((0,0), day, font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2, y+5), day, font=fday, fill=WHITE)
        if i > 0:
            d.line([(x,y),(x,y+21)], fill=BLUE, width=1)
    y += 22

    for ri, cat in enumerate(CATS):
        rh = ROW_HEIGHTS[cat]
        d.rectangle([(0,y),(W,y+rh-1)], fill=R_ODD if ri%2==0 else R_EVEN)
        d.line([(0,y),(W,y)], fill=GRID, width=1)

        for i, day in enumerate(days):
            x = i * dw
            if i > 0:
                d.line([(x,y),(x,y+rh)], fill=GRID, width=1)

            items = [it for it in week_data.get(day,[]) if it["kategorie"]==cat]
            if not items:
                b = d.textbbox((0,0),"–",font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-6),"–",font=ftxt,fill=(180,180,180))
                continue

            it = items[0]
            cx, cy = x+4, y+4
            avw = dw-8

            d.text((cx,cy), it["kategorie"], font=fcat, fill=(100,100,100))
            cy += 11

            if it["vv"]:
                bl = "Vegan" if it["vv"]=="VG" else "Veg."
                bc = C_VG   if it["vv"]=="VG" else C_V
                b  = d.textbbox((0,0),bl,font=fbdg)
                bw = b[2]-b[0]+6; bh = b[3]-b[1]+3
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh)],radius=3,fill=bc)
                d.text((cx+3,cy+1),bl,font=fbdg,fill=WHITE)
                cy += bh+3

            max_lines = 2 if cat=="Suppe" else 3
            for ln in wrap_text(d, it["name"], ftxt, avw)[:max_lines]:
                d.text((cx,cy),ln,font=ftxt,fill=C_TXT)
                cy += 12

            if it["preis_int"]:
                pl = f"Int: {it['preis_int']}"
                b  = d.textbbox((0,0),pl,font=fprc)
                d.text((x+dw-(b[2]-b[0])-4, y+rh-(b[3]-b[1])-4),
                       pl,font=fprc,fill=LIGHT)
        y += rh

    d.line([(0,y),(W,y)], fill=GRID, width=1)
    y += 1

    if y+18 < H-25:
        d.rectangle([(0,y),(W,y+18)], fill=(245,249,253))
        d.rectangle([( 6,y+4),(18,y+14)], fill=C_VG)
        d.text((22,y+4),"Vegan",        font=fprc, fill=C_TXT)
        d.rectangle([(68,y+4),(80,y+14)], fill=C_V)
        d.text((84,y+4),"Vegetarisch",  font=fprc, fill=C_TXT)
        d.text((170,y+4),"Int = Mitarbeiterpreis", font=fprc, fill=(120,120,120))

    _footer(d, kw, label, local_dt, fftr)
    return img

def _footer(d, kw, label, local_dt, f):
    txt = (f"KW {kw:02d} / {label}  –  "
           f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  –  siemens.cateringportal.io")
    d.rectangle([(0,H-24),(W,H)], fill=BLUE)
    b = d.textbbox((0,0),txt,font=f)
    d.text(((W-(b[2]-b[0]))//2,H-17),txt,font=f,fill=WHITE)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now   = datetime.now(timezone.utc)
    local = german_time(now)
    label, kw = kw_label(now)
    out_path  = OUT_DIR / f"kantine_{label}.jpg"

    print(f"Target URL : {URL_MENU}")
    print(f"Week label : {label}  (KW {kw:02d})")

    # ── 1. Load HTML with Playwright, find PDF link ──
    print("Loading page to find PDF link...")
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page    = browser.new_page(viewport={"width": 1200, "height": 900})
        page.goto(URL_MENU, wait_until="load", timeout=60000)
        page.wait_for_timeout(3000)
        print(f"  Page title: {page.title()}")
        html = page.content()
        browser.close()

    print(f"  HTML size: {len(html):,} bytes")

    pdf_url = find_pdf_url(html, kw)
    if not pdf_url:
        print("ERROR: No PDF URL found in page HTML.")
        print("  Rendering placeholder image.")
        img = render({}, kw, label, local)
        img.save(str(out_path), "JPEG", quality=92)
        print(f"Saved placeholder: {out_path}")
        return

    # ── 2. Download PDF ──
    print(f"Downloading PDF: {pdf_url[:80]}...")
    pdf_bytes = download_pdf(pdf_url)

    # ── 3. Extract text from PDF ──
    print("Extracting text from PDF...")
    pdf_text = extract_pdf_text(pdf_bytes)
    # Debug: first 500 chars
    print("  PDF text preview:")
    for line in pdf_text.splitlines()[:20]:
        print(f"    {line}")

    # ── 4. Parse menu ──
    print("Parsing menu...")
    week_data = parse_menu_from_text(pdf_text, local)
    print(f"  Days parsed: {list(week_data.keys())}")
    for day, meals in week_data.items():
        print(f"  {day}:")
        for m in meals:
            print(f"    [{m['kategorie']}] {m['name'][:60]} | vv={m['vv']} | {m['preis_int']}")

    # ── 5. Render JPEG ──
    img = render(week_data, kw, label, local)
    img.save(str(out_path), "JPEG", quality=92)
    print(f"Saved: {out_path}  ({img.size[0]}x{img.size[1]})")

    # ── 6. Cleanup ──
    for old in sorted(OUT_DIR.glob("kantine_*.jpg"))[:-MAX_KEEP]:
        old.unlink()
        print(f"Removed: {old}")


if __name__ == "__main__":
    main()
