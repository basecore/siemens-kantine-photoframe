#!/usr/bin/env python3
"""Siemens Kantine Regensburg – weekly menu PDF scraper + 800x600 JPEG renderer.

The qnips PDF has a column layout:
  - All 5 soups listed consecutively (Mon-Fri)
  - Then all 5 Essen-1 dishes (Mon-Fri)
  - Then all 5 Essen-2 dishes (Mon-Fri)
  - Then all 5 Essen-3 dishes (Mon-Fri)
  - Text is extracted as one long string with items separated by price pairs

Approach:
  1. Load cateringportal.io with Playwright -> find qnips PDF link
  2. Download the weekly PDF with requests
  3. Parse menu items from PDF text (dedup + price-pair splitting)
  4. Assign items to days (5 per category = index 0-4 -> Mon-Fri)
  5. Render clean 800x600 LANDSCAPE JPEG with Pillow
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

# LANDSCAPE 800x600 for Philips 8FF3WMI
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

# ── Step 1: Find qnips PDF URL in page HTML ────────────────────────────────────
def find_pdf_url(html: str, kw: int) -> str | None:
    # Strict: only Mittagessen PDFs from qnips
    strict = re.findall(
        r'(https://files\.qnips\.com/release-menu-pdfs/Mittagessen[^"\s\'<>]+\.pdf[^"\s\'<>]*)',
        html, re.I
    )
    if strict:
        for url in strict:
            if f"_DE_{kw}_" in url or f"_{kw}_" in url:
                print(f"  PDF found (KW {kw} match): {url[:120]}")
                return url
        print(f"  PDF found (first Mittagessen match): {strict[0][:120]}")
        return strict[0]

    # Fallback: any qnips PDF
    loose = re.findall(
        r'(https://files\.qnips\.com/[^"\s\'<>]+\.pdf[^"\s\'<>]*)',
        html, re.I
    )
    for url in loose:
        if f"_{kw}_" in url:
            print(f"  PDF found (qnips KW match): {url[:120]}")
            return url
    if loose:
        print(f"  PDF found (qnips first): {loose[0][:120]}")
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

# ── Step 3: Extract raw text from PDF ─────────────────────────────────────────────
def extract_pdf_text(pdf_bytes: bytes) -> str:
    if pdfplumber:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                # join all pages, no layout mode (gives single long string per page)
                pages = [p.extract_text() or "" for p in pdf.pages]
            text = "\n".join(pages)
            if text.strip():
                print(f"  pdfplumber: {len(text)} chars")
                return text
        except Exception as e:
            print(f"  pdfplumber error: {e}")
    if PdfReader:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        print(f"  pypdf: {len(text)} chars")
        return text
    raise RuntimeError("No PDF parser available")

# ── Step 4: Parse qnips column layout ─────────────────────────────────────────────
def dedup_text(text: str) -> str:
    """Remove immediately repeated words/phrases that pdfplumber creates from dual columns."""
    # e.g. "Rinderbrühe mitRinderbrühe mit" -> "Rinderbrühe mit"
    for _ in range(6):
        text = re.sub(r'([A-Za-z\u00c0-\u017e,\- ]{4,})\1', r'\1', text)
    return text

def fix_name(name: str) -> str:
    """Insert spaces before CamelCase transitions and clean up whitespace."""
    # "CurrysoßeCurrysoße" already handled by dedup
    # "TomatenspaghettiZitronenecke" -> "Tomatenspaghetti Zitronenecke"
    name = re.sub(r'([a-z\u00e0-\u017e])([A-Z\u00c0-\u00d6\u00d8-\u00de])', r'\1 \2', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def detect_vv(name: str) -> str:
    low = name.lower()
    if 'vegan' in low:
        return 'VG'
    if 'vegetar' in low:
        return 'V'
    return ''

def split_items(body: str) -> list:
    """
    Split the PDF body text into individual dish items.
    Each item ends with a price pair like "3,206,40" or "0,601,20".
    Returns list of (name, int_price_str).
    """
    # Split at price pairs (intXX,XXextXX,XX merged together)
    # Pattern: digits,digits directly followed by another digits,digits
    # We split AFTER each price pair to get: name + price_pair | next_name...
    parts = re.split(r'(?<=\d{2})  +(?=[A-Z\u00c0-\u00de])', body)

    result = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Extract trailing price pair "3,206,40" or "0,601,20" (no space between int+ext)
        m = re.search(r'(\d+,\d{2})(\d+,\d{2})\s*$', part)
        if m:
            name = part[:m.start()].strip()
            price = m.group(1) + ' \u20ac'
        else:
            # Try with space between: "3,20 6,40"
            m2 = re.search(r'(\d+,\d{2})\s+(\d+,\d{2})\s*$', part)
            if m2:
                name = part[:m2.start()].strip()
                price = m2.group(1) + ' \u20ac'
            else:
                name = part
                price = ''

        name = fix_name(name)
        # skip noise
        if not name or len(name) < 3:
            continue
        low = name.lower()
        skip_words = ['oder', 'int', 'ext', 'int.ext', 'mo - fr', 'alle preise',
                      'all prices', 'restaurant', 'thomas', '+49', 'allergen']
        if any(low.startswith(w) for w in skip_words):
            continue
        # skip pure price / header lines
        if re.match(r'^[\d.,\s\u20ac/]+$', name):
            continue

        result.append((name, price))

    return result

def parse_menu(pdf_text: str, local_dt: datetime) -> dict:
    """
    Parse the qnips PDF text into week_data dict.
    Layout: items 0-4 = Suppe Mon-Fri, 5-9 = Essen1, 10-14 = Essen2, 15-19 = Essen3.
    """
    monday = local_dt - timedelta(days=local_dt.weekday())
    day_short = ['Mo', 'Di', 'Mi', 'Do', 'Fr']
    days = [f"{day_short[i]} {(monday + timedelta(days=i)).strftime('%d.%m')}" for i in range(5)]

    # Clean and flatten text
    text = ' '.join(pdf_text.split())
    text = dedup_text(text)

    # Strip header up to phone number or known markers
    text = re.sub(
        r'^.*?(?:Restaurant\s+Regensburg|\+49[^\s]+|\d{2}:\d{2}\s+Uhr)\s*',
        '', text, flags=re.I
    ).strip()

    # Now split into items
    items = split_items(text)

    print(f"  Items extracted: {len(items)}")
    for i, (n, p) in enumerate(items):
        print(f"    {i:2d}: {n[:60]!r:65} {p}")

    # Map to days: 5 items per category
    CATS = ['Suppe', 'Essen 1', 'Essen 2', 'Essen 3']
    week_data = {day: [] for day in days}

    for ci, cat in enumerate(CATS):
        start = ci * 5
        cat_items = items[start:start + 5]
        for di, day in enumerate(days):
            if di < len(cat_items):
                name, price = cat_items[di]
                week_data[day].append({
                    'kategorie': cat,
                    'name':      name,
                    'vv':        detect_vv(name),
                    'preis_int': price,
                })

    # Remove empty days
    week_data = {k: v for k, v in week_data.items() if v}
    return week_data

# ── Step 5: Render 800x600 landscape JPEG ─────────────────────────────────────────
def wrap_text(draw, text, f, max_w):
    words = text.split()
    out, cur = [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
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

CATS = ['Suppe', 'Essen 1', 'Essen 2', 'Essen 3']

def render(week_data, kw, label, local_dt):
    img  = Image.new('RGB', (W, H), (255, 255, 255))
    d    = ImageDraw.Draw(img)
    ftit = lf(14, True)
    fday = lf(11, True)
    fcat = lf( 8, True)
    ftxt = lf( 9, False)
    fbdg = lf( 8, True)
    fprc = lf( 8, False)
    fftr = lf( 9, False)

    HDR_H    = 36
    FOOTER_H = 22
    LEGEND_H = 17

    # Header
    d.rectangle([(0,0),(W,HDR_H)], fill=BLUE)
    title = f'Siemens Kantine Regensburg  |  KW {kw:02d}'
    b = d.textbbox((0,0), title, font=ftit)
    d.text(((W-(b[2]-b[0]))//2, (HDR_H-(b[3]-b[1]))//2), title, font=ftit, fill=WHITE)
    y = HDR_H

    if not week_data:
        d.text((20, y+40), 'Speiseplan konnte nicht geladen werden.', font=ftxt, fill=C_TXT)
        d.text((20, y+60), 'Bitte manuell pr\u00fcfen:', font=ftxt, fill=C_TXT)
        d.text((20, y+80), URL_MENU, font=ftxt, fill=LIGHT)
        _footer(d, kw, label, local_dt, fftr)
        return img

    days = list(week_data.keys())[:5]
    n    = len(days)
    dw   = W // n

    # Day headers
    DAY_H = 20
    for i, day in enumerate(days):
        x = i * dw
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)], fill=LIGHT)
        b = d.textbbox((0,0), day, font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2, y+(DAY_H-(b[3]-b[1]))//2), day, font=fday, fill=WHITE)
        if i > 0:
            d.line([(x,y),(x,y+DAY_H)], fill=BLUE, width=1)
    y += DAY_H

    # Distribute remaining height among rows
    avail = H - y - FOOTER_H - LEGEND_H - 2
    row_suppe  = int(avail * 0.20)
    row_essen  = (avail - row_suppe) // 3
    ROW_H = {
        'Suppe':   row_suppe,
        'Essen 1': row_essen,
        'Essen 2': row_essen,
        'Essen 3': avail - row_suppe - 2*row_essen,
    }

    for ri, cat in enumerate(CATS):
        rh = ROW_H[cat]
        d.rectangle([(0,y),(W,y+rh-1)], fill=R_ODD if ri%2==0 else R_EVEN)
        d.line([(0,y),(W,y)], fill=GRID, width=1)

        for i, day in enumerate(days):
            x = i * dw
            if i > 0:
                d.line([(x,y),(x,y+rh)], fill=GRID, width=1)

            items = [it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items:
                b = d.textbbox((0,0),'-',font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2, y+rh//2-6), '-', font=ftxt, fill=(180,180,180))
                continue

            it  = items[0]
            cx  = x + 4
            cy  = y + 3
            avw = dw - 8

            d.text((cx,cy), it['kategorie'], font=fcat, fill=(100,100,100))
            cy += 10

            if it['vv']:
                bl = 'Vegan' if it['vv']=='VG' else 'Veg.'
                bc = C_VG   if it['vv']=='VG' else C_V
                b  = d.textbbox((0,0),bl,font=fbdg)
                bw = b[2]-b[0]+5; bh = b[3]-b[1]+3
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh)],radius=3,fill=bc)
                d.text((cx+3,cy+1),bl,font=fbdg,fill=WHITE)
                cy += bh + 2

            max_lines = 2 if cat=='Suppe' else 3
            for ln in wrap_text(d, it['name'], ftxt, avw)[:max_lines]:
                d.text((cx,cy),ln,font=ftxt,fill=C_TXT)
                cy += 11

            if it['preis_int']:
                pl = f"Int: {it['preis_int']}"
                b  = d.textbbox((0,0),pl,font=fprc)
                d.text((x+dw-(b[2]-b[0])-3, y+rh-(b[3]-b[1])-3),
                       pl, font=fprc, fill=LIGHT)
        y += rh

    d.line([(0,y),(W,y)], fill=GRID, width=1)
    y += 1

    # Legend
    d.rectangle([(0,y),(W,y+LEGEND_H)], fill=(245,249,253))
    d.rectangle([( 5,y+4),(15,y+13)], fill=C_VG)
    d.text((19,y+3), 'Vegan',         font=fprc, fill=C_TXT)
    d.rectangle([(65,y+4),(75,y+13)], fill=C_V)
    d.text((79,y+3), 'Vegetarisch',   font=fprc, fill=C_TXT)
    d.text((175,y+3), 'Int = Mitarbeiterpreis', font=fprc, fill=(120,120,120))

    _footer(d, kw, label, local_dt, fftr)
    return img

def _footer(d, kw, label, local_dt, f):
    txt = (f'KW {kw:02d} / {label}  \u2013  '
           f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  \u2013  siemens.cateringportal.io")
    d.rectangle([(0,H-FOOTER_H),(W,H)], fill=BLUE)
    b = d.textbbox((0,0),txt,font=f)
    d.text(((W-(b[2]-b[0]))//2,H-16),txt,font=f,fill=WHITE)

FOOTER_H = 22

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now   = datetime.now(timezone.utc)
    local = german_time(now)
    label, kw = kw_label(now)
    out_path  = OUT_DIR / f'kantine_{label}.jpg'

    print(f'Target URL : {URL_MENU}')
    print(f'Week label : {label}  (KW {kw:02d})')

    # 1. Load HTML, find PDF URL
    print('Loading page to find PDF link...')
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page    = browser.new_page(viewport={'width': 1200, 'height': 900})
        page.goto(URL_MENU, wait_until='load', timeout=60000)
        page.wait_for_timeout(3000)
        print(f'  Page title: {page.title()}')
        html = page.content()
        browser.close()
    print(f'  HTML size: {len(html):,} bytes')

    pdf_url = find_pdf_url(html, kw)
    if not pdf_url:
        print('ERROR: No qnips PDF URL found -> rendering placeholder.')
        img = render({}, kw, label, local)
        img.save(str(out_path), 'JPEG', quality=92)
        print(f'Saved placeholder: {out_path}')
        return

    # 2. Download PDF
    print(f'Downloading PDF...')
    pdf_bytes = download_pdf(pdf_url)

    # 3. Extract text
    print('Extracting PDF text...')
    pdf_text = extract_pdf_text(pdf_bytes)

    # 4. Parse menu
    print('Parsing menu items...')
    week_data = parse_menu(pdf_text, local)
    print(f'Days parsed: {list(week_data.keys())}')

    # 5. Render
    img = render(week_data, kw, label, local)
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    # 6. Cleanup
    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink()
        print(f'Removed: {old}')


if __name__ == '__main__':
    main()
