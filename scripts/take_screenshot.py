#!/usr/bin/env python3
"""Siemens Kantine Regensburg – weekly menu PDF scraper + 800x600 JPEG renderer.

Approach:
  1. Load cateringportal.io with Playwright – find the qnips PDF link
     (prefer files.qnips.com/release-menu-pdfs/Mittagessen*)
  2. Download the weekly PDF with requests
  3. Parse menu text from PDF with pdfplumber
  4. Render clean 800x600 LANDSCAPE JPEG with Pillow
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

# LANDSCAPE for Philips 8FF3WMI
W, H = 800, 600

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

# ── Step 1: Find PDF URL ─────────────────────────────────────────────────────────
def find_pdf_url(html: str, kw: int) -> str | None:
    """
    Search the page HTML for a qnips PDF link.
    Only accept URLs from files.qnips.com that contain 'Mittagessen' in the filename.
    """
    # Strict: only qnips release-menu PDFs
    strict = re.findall(
        r'(https://files\.qnips\.com/release-menu-pdfs/Mittagessen[^"\s\'<>]+\.pdf[^"\s\'<>]*)',
        html, re.I
    )
    if strict:
        # prefer current KW
        for url in strict:
            if f"_{kw}_" in url or f"_DE_{kw}_" in url:
                print(f"  PDF found (KW {kw} match): {url[:120]}")
                return url
        print(f"  PDF found (first qnips match): {strict[0][:120]}")
        return strict[0]

    # Fallback: any qnips PDF
    loose = re.findall(
        r'(https://files\.qnips\.com/[^"\s\'<>]+\.pdf[^"\s\'<>]*)',
        html, re.I
    )
    for url in loose:
        if f"_{kw}_" in url:
            print(f"  PDF found (qnips loose KW match): {url[:120]}")
            return url
    if loose:
        print(f"  PDF found (qnips loose first): {loose[0][:120]}")
        return loose[0]

    print("  No qnips PDF URL found in HTML.")
    return None

# ── Step 2: Download PDF ─────────────────────────────────────────────────────────
def download_pdf(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; KantinoBot/1.0)"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    print(f"  PDF downloaded: {len(r.content):,} bytes")
    return r.content

# ── Step 3: Extract text from PDF ────────────────────────────────────────────────
def extract_pdf_text(pdf_bytes: bytes) -> str:
    if pdfplumber:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = []
                for page in pdf.pages:
                    # extract_text with layout preserves columns better
                    t = page.extract_text(layout=True) or page.extract_text() or ""
                    pages.append(t)
            text = "\n".join(pages)
            if text.strip():
                print(f"  pdfplumber: {len(text)} chars extracted")
                return text
        except Exception as e:
            print(f"  pdfplumber error: {e}")
    if PdfReader:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [p.extract_text() or "" for p in reader.pages]
        text = "\n".join(pages)
        print(f"  pypdf: {len(text)} chars extracted")
        return text
    raise RuntimeError("No PDF parser available")

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
    ("Suppe",   re.compile(r"^\s*(suppe|vorspeise|tagessuppe|cremesuppe)", re.I)),
    ("Essen 1", re.compile(r"^\s*(essen\s*1|men.\s*1|gericht\s*1|men.\s*i\b|hauptgericht\s*1)", re.I)),
    ("Essen 2", re.compile(r"^\s*(essen\s*2|men.\s*2|gericht\s*2|men.\s*ii\b|hauptgericht\s*2)", re.I)),
    ("Essen 3", re.compile(r"^\s*(essen\s*3|men.\s*3|gericht\s*3|men.\s*iii\b|hauptgericht\s*3|vegetarisch|vegan)", re.I)),
]
PRICE_RE = re.compile(r"(\d+[,.]\d{2})\s*\u20ac?")

def detect_day(line):
    m = DAY_RE.match(line.strip())
    if not m:
        return None
    key = m.group(1).lower().rstrip(".")
    return DAY_MAP.get(key)

def detect_cat(line):
    for cat, pat in CAT_RE:
        if pat.search(line):
            return cat
    return None

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

def is_noise(line):
    """Skip lines that are clearly not menu content."""
    low = line.lower()
    noise_words = [
        "allergen", "additiv", "zusatzstoff", "kennzeichn",
        "zf ", "zk ", "za ", "zgv", "gluten",
        "nuts", "peanut", "crustacean", "lupin", "mollusc",
        "milk", "celery", "mustard", "sesame",
        "calories", "kalorien", "kcal", "kj",
        "siemens ag", "siemens gastronomie",
        "montag bis freitag", "monday", "tuesday",
        "printed", "gedruckt", "kantine regensburg",
        "impressum", "datenschutz", "copyright",
        "seite ", "page ",
    ]
    if any(w in low for w in noise_words):
        return True
    # pure number / price lines without letters
    if re.match(r"^[\d.,\s\u20ac/]+$", line):
        return True
    # very short lines (1-2 chars)
    if len(line.strip()) <= 2:
        return True
    return False

def parse_menu_from_text(text: str, local_dt: datetime) -> dict:
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
    cur_day  = None
    cur_cat  = None
    buf: list = []

    def flush():
        nonlocal cur_cat, buf
        if cur_day and cur_cat and buf:
            txt = " ".join(buf).strip()
            if len(txt) > 4:
                label = f"{cur_day} {dates.get(cur_day, '')}"
                week.setdefault(label, [])
                if not any(e["kategorie"] == cur_cat for e in week[label]):
                    week[label].append({
                        "kategorie": cur_cat,
                        "name":      txt,
                        "vv":        detect_vv(txt),
                        "preis_int": extract_price(txt),
                    })
        buf.clear()
        cur_cat = None

    for line in lines:
        if is_noise(line):
            continue

        day = detect_day(line)
        if day:
            flush()
            cur_day = day
            cur_cat = None
            continue

        cat = detect_cat(line)
        if cat:
            flush()
            cur_cat = cat
            # strip category label from line, keep rest if meaningful
            rest = re.sub(
                r"(suppe|vorspeise|tagessuppe|cremesuppe"
                r"|essen\s*\d+|men.\s*\d+|gericht\s*\d+"
                r"|men.\s*i{1,3}\b|hauptgericht\s*\d+"
                r"|vegetarisch|vegan)",
                "", line, flags=re.I
            ).strip(" :|-").strip()
            buf = [rest] if len(rest) > 4 else []
            continue

        if cur_cat and len(line) > 3:
            buf.append(line)

    flush()

    for label in list(week.keys()):
        week[label] = week[label][:4]

    return week

# ── Step 5: Render landscape JPEG 800x600 ─────────────────────────────────────────
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

CATS = ["Suppe", "Essen 1", "Essen 2", "Essen 3"]

def render(week_data, kw, label, local_dt):
    img  = Image.new("RGB", (W, H), (255, 255, 255))
    d    = ImageDraw.Draw(img)
    ftit = lf(14, True)
    fday = lf(11, True)
    fcat = lf( 8, True)
    ftxt = lf( 9, False)
    fbdg = lf( 8, True)
    fprc = lf( 8, False)
    fftr = lf( 9, False)

    # ── Header ──
    HDR_H = 38
    d.rectangle([(0,0),(W,HDR_H)], fill=BLUE)
    title = f"Siemens Kantine Regensburg  |  KW {kw:02d}"
    b = d.textbbox((0,0), title, font=ftit)
    d.text(((W-(b[2]-b[0]))//2, (HDR_H-(b[3]-b[1]))//2), title, font=ftit, fill=WHITE)
    y = HDR_H

    if not week_data:
        d.text((20, y+40), "Speiseplan konnte nicht geladen werden.", font=ftxt, fill=C_TXT)
        d.text((20, y+60), "Bitte manuell prüfen:",                  font=ftxt, fill=C_TXT)
        d.text((20, y+80), URL_MENU,                                  font=ftxt, fill=LIGHT)
        _footer(d, kw, label, local_dt, fftr)
        return img

    days = list(week_data.keys())[:5]
    n    = len(days)
    dw   = W // n                 # column width per day

    # ── Day-header row ──
    DAY_H = 22
    for i, day in enumerate(days):
        x = i * dw
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)], fill=LIGHT)
        b = d.textbbox((0,0), day, font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2, y+(DAY_H-(b[3]-b[1]))//2), day, font=fday, fill=WHITE)
        if i > 0:
            d.line([(x,y),(x,y+DAY_H)], fill=BLUE, width=1)
    y += DAY_H

    # Row heights to fill remaining space (H - header - day-row - footer)
    FOOTER_H = 24
    LEGEND_H = 18
    available = H - y - FOOTER_H - LEGEND_H - 1
    n_cats    = len(CATS)
    # Suppe gets 60%, Essen rows split the rest equally
    row_h_suppe  = int(available * 0.22)
    row_h_essen  = (available - row_h_suppe) // (n_cats - 1)
    ROW_H = {
        "Suppe":   row_h_suppe,
        "Essen 1": row_h_essen,
        "Essen 2": row_h_essen,
        "Essen 3": available - row_h_suppe - 2*row_h_essen,
    }

    for ri, cat in enumerate(CATS):
        rh = ROW_H[cat]
        d.rectangle([(0,y),(W,y+rh-1)], fill=R_ODD if ri%2==0 else R_EVEN)
        d.line([(0,y),(W,y)], fill=GRID, width=1)

        for i, day in enumerate(days):
            x = i * dw
            if i > 0:
                d.line([(x,y),(x,y+rh)], fill=GRID, width=1)

            items = [it for it in week_data.get(day,[]) if it["kategorie"]==cat]
            if not items:
                b = d.textbbox((0,0),"–",font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2, y+rh//2-6),"–",font=ftxt,fill=(180,180,180))
                continue

            it = items[0]
            cx, cy = x+4, y+3
            avw = dw - 8

            # category label
            d.text((cx,cy), it["kategorie"], font=fcat, fill=(100,100,100))
            cy += 11

            # badge
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
                d.text((x+dw-(b[2]-b[0])-4, y+rh-(b[3]-b[1])-3),
                       pl,font=fprc,fill=LIGHT)
        y += rh

    d.line([(0,y),(W,y)], fill=GRID, width=1)
    y += 1

    # Legend
    d.rectangle([(0,y),(W,y+LEGEND_H)], fill=(245,249,253))
    d.rectangle([( 6,y+4),(18,y+14)], fill=C_VG)
    d.text((22,y+4), "Vegan",          font=fprc, fill=C_TXT)
    d.rectangle([(72,y+4),(84,y+14)], fill=C_V)
    d.text((88,y+4), "Vegetarisch",    font=fprc, fill=C_TXT)
    d.text((190,y+4), "Int = Mitarbeiterpreis", font=fprc, fill=(120,120,120))

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

    # 1. Load HTML
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
        print("ERROR: No qnips PDF URL found – rendering placeholder.")
        img = render({}, kw, label, local)
        img.save(str(out_path), "JPEG", quality=92)
        print(f"Saved placeholder: {out_path}")
        return

    # 2. Download PDF
    print(f"Downloading PDF...")
    pdf_bytes = download_pdf(pdf_url)

    # 3. Extract text
    print("Extracting text from PDF...")
    pdf_text = extract_pdf_text(pdf_bytes)
    print("  PDF text preview (first 30 lines):")
    for line in pdf_text.splitlines()[:30]:
        if line.strip():
            print(f"    {repr(line)}")

    # 4. Parse menu
    print("Parsing menu...")
    week_data = parse_menu_from_text(pdf_text, local)
    print(f"  Days parsed: {list(week_data.keys())}")
    for day, meals in week_data.items():
        print(f"  {day}:")
        for m in meals:
            print(f"    [{m['kategorie']}] {m['name'][:70]} | vv={m['vv']} | {m['preis_int']}")

    # 5. Render JPEG
    img = render(week_data, kw, label, local)
    img.save(str(out_path), "JPEG", quality=92)
    print(f"Saved: {out_path}  ({img.size[0]}x{img.size[1]})")

    # 6. Cleanup
    for old in sorted(OUT_DIR.glob("kantine_*.jpg"))[:-MAX_KEEP]:
        old.unlink()
        print(f"Removed: {old}")


if __name__ == "__main__":
    main()
