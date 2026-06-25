#!/usr/bin/env python3
"""Siemens Kantine Regensburg - German menu -> 800x600 JPEG.

Strategy:
  1. Query qnips REST API directly to get German PDF URL
  2. Playwright intercept (fallback PDF discovery)
  3. Download + parse German PDF  (5x Suppe, 5x Essen1, 5x Essen2, 5x Essen3)
  4. DOM fallback: parse tab/column structure via BeautifulSoup CSS selectors
     - Each day tab contains its own Food1/Food2/Food3 entries
     - Dishes within a day are separated by Int-price tokens
"""
import io, os, re, requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup

try:
    import pdfplumber
except ImportError:
    pdfplumber = None
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

# ── Config ───────────────────────────────────────────────────────────────────
URL_BASE = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID     = os.environ.get("CATERINGPORTAL_SID", "").strip()
STORE_ID = "41196"  # qnips store ID for Regensburg

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8
W, H     = 800, 600
FOOTER_H = 22

# ── Colours ──────────────────────────────────────────────────────────────────
BLUE  = (0, 57, 107);   LIGHT = (0, 119, 193)
R_ODD = (240, 246, 252); R_EVEN = (255, 255, 255)
C_VG  = (34, 139, 34);  C_V   = (100, 180, 60)
C_TXT = (30, 30, 30);   WHITE  = (255, 255, 255)
GRID  = (200, 215, 230)

_FREG = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
_FBOL = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]

def lf(size, bold=False):
    for p in (_FBOL if bold else _FREG) + _FREG:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except OSError: pass
    return ImageFont.load_default()

# ── Time ─────────────────────────────────────────────────────────────────────
def german_time(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Berlin"))
    except ImportError: pass
    import calendar
    yr = dt.year
    def last_sun(y, m):
        ld = calendar.monthrange(y, m)[1]
        d  = datetime(y, m, ld, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday()+1) % 7)
    cs = last_sun(yr, 3).replace(hour=1)
    ce = last_sun(yr, 10).replace(hour=1)
    return dt + timedelta(hours=2 if cs <= dt < ce else 1)

def kw_label(dt):
    d = german_time(dt)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", int(w)

def week_dates(local_dt):
    monday = local_dt - timedelta(days=local_dt.weekday())
    return [(monday + timedelta(days=i)).strftime('%d.%m') for i in range(5)]

def day_keys(local_dt):
    short = ['Mo', 'Di', 'Mi', 'Do', 'Fr']
    return [f"{short[i]} {d}" for i, d in enumerate(week_dates(local_dt))]

# ── Step 1a: qnips API -> German PDF URL ─────────────────────────────────────
def qnips_pdf_url(kw: int, local_dt: datetime) -> str | None:
    """Try several qnips API endpoints to get the German PDF download link."""
    monday   = local_dt - timedelta(days=local_dt.weekday())
    date_str = monday.strftime("%Y-%m-%d")
    hdrs     = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    endpoints = [
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{STORE_ID}/MenuPdfs?date={date_str}",
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{STORE_ID}/MenuPdfs?date={date_str}&language=de",
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{STORE_ID}/MenuPdfs",
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{STORE_ID}/Menus?date={date_str}",
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{STORE_ID}/MenuWeeks?kw={kw}",
    ]
    pdf_re = re.compile(
        r'https://files\.qnips\.com/[^\s"<>\']+Mittagessen_DE[^\s"<>\']*\.pdf[^\s"<>\']?',
        re.I)
    for ep in endpoints:
        try:
            r = requests.get(ep, headers=hdrs, timeout=10)
            print(f"  [API] {ep[-60:]} -> {r.status_code}")
            if r.status_code == 200:
                hits = pdf_re.findall(r.text)
                if hits:
                    print(f"  [API PDF] {hits[0][:100]}")
                    return hits[0]
        except Exception as e:
            print(f"  [API] error: {e}")
    return None

# ── Step 1b: Playwright page load + intercept ─────────────────────────────────
def load_page(url: str):
    """Returns (html_str, pdf_url_or_None)."""
    found_pdf = []
    pdf_re = re.compile(r'Mittagessen.*\.pdf', re.I)

    def _check(u):
        if pdf_re.search(u) and 'allergen' not in u.lower() and u not in found_pdf:
            print(f"  [intercept PDF] {u[:120]}")
            found_pdf.append(u)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )

        def on_response(resp):
            _check(resp.url)
            if 'qnips' in resp.url:
                ct = resp.headers.get('content-type', '')
                if 'json' in ct or 'text' in ct:
                    try:
                        body = resp.text()
                        for u in re.findall(
                            r'https://files\.qnips\.com/[^\s"<>\']+\.pdf[^\s"<>\']*',
                            body, re.I):
                            _check(u)
                    except Exception:
                        pass

        page.on("response", on_response)
        page.goto(url, wait_until="load", timeout=60000)
        print("  Waiting for menu...")
        try:
            page.wait_for_selector("text=Food 1", timeout=12000)
            print("  'Food 1' found")
        except Exception:
            try: page.wait_for_selector("text=Essen 1", timeout=5000)
            except Exception: page.wait_for_timeout(8000)
        page.wait_for_timeout(3000)
        print(f"  Title: {page.title()}")
        html = page.content()
        browser.close()

    # scan raw HTML too
    for u in re.findall(
        r'https://files\.qnips\.com/[^\s"<>\']+Mittagessen[^\s"<>\']+\.pdf[^\s"<>\']*',
        html, re.I):
        _check(u)

    print(f"  HTML: {len(html):,} bytes  |  PDF intercepted: {bool(found_pdf)}")
    return html, found_pdf[0] if found_pdf else None

# ── Step 2: Download + parse German PDF ──────────────────────────────────────
def download_pdf(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    print(f"  PDF: {len(r.content):,} bytes")
    return r.content

def extract_pdf_text(pdf_bytes: bytes) -> str:
    if pdfplumber:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                txt = '\n'.join(p.extract_text(x_tolerance=2, y_tolerance=2) or '' for p in pdf.pages)
            if txt.strip():
                print(f"  pdfplumber ok: {len(txt)} chars")
                return txt
        except Exception as e:
            print(f"  pdfplumber: {e}")
    if PdfReader:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        txt = '\n'.join(p.extract_text() or '' for p in reader.pages)
        print(f"  pypdf ok: {len(txt)} chars")
        return txt
    raise RuntimeError("No PDF parser")

DEDUP2   = re.compile(r'\b(\S{3,}.{0,40})\1', re.S)
PRICE_RE = re.compile(r'(\d{1,2},\d{2})(\d{1,2},\d{2})|(\d{1,2},\d{2})\s+(\d{1,2},\d{2})')
NOISE_RE = re.compile(
    r'int\.ext\.|Mo\s*-\s*Fr|\d{2}:\d{2}\s*Uhr|Restaurant\s+Regensburg'
    r'|Thomas\s+\w+|\+49|Alle Preise|All prices|oder|Montag|Dienstag'
    r'|Mittwoch|Donnerstag|Freitag|\d{2}\.\d{2}\.', re.I)

def dedup(s):
    for _ in range(10):
        s2 = DEDUP2.sub(r'\1', s)
        if s2 == s: break
        s = s2
    s = re.sub(r'([a-z\u00e4\u00f6\u00fc\u00df])([A-Z\u00c4\u00d6\u00dc])', r'\1 \2', s)
    return re.sub(r'\s+', ' ', s).strip()

def parse_pdf(pdf_text: str, local_dt: datetime) -> dict:
    keys  = day_keys(local_dt)
    items = []
    for line in pdf_text.splitlines():
        line = line.strip()
        if not line: continue
        m = PRICE_RE.search(line)
        if not m: continue
        p_int    = (m.group(1) or m.group(3)).replace(',', '.') + ' \u20ac'
        raw_name = line[:m.start()].strip()
        name     = dedup(raw_name)
        if len(name) < 3 or NOISE_RE.search(name): continue
        if re.match(r'^[\d,.\s\u20ac/]+$', name): continue
        low = name.lower()
        items.append({'name': name, 'preis_int': p_int,
                      'vv': 'VG' if 'vegan' in low else ('V' if 'vegetar' in low else '')})

    print(f"  PDF items: {len(items)}")
    for i, it in enumerate(items):
        print(f"    {i:2d}: {it['name'][:55]!r:58} {it['preis_int']}")

    CATS = ['Suppe', 'Essen 1', 'Essen 2', 'Essen 3']
    week_data = {k: [] for k in keys}
    for ci, cat in enumerate(CATS):
        for di, key in enumerate(keys):
            idx = ci * 5 + di
            if idx < len(items):
                week_data[key].append({'kategorie': cat, **items[idx]})
    return {k: v for k, v in week_data.items() if v}

# ── Step 3: DOM fallback ──────────────────────────────────────────────────────
# The SPA renders a tab per weekday. Each tab contains rows:
#   Soup / Starter, Food 1, Food 2, Food 3
# We try:
#   A) CSS: tab panels that contain dates
#   B) Flat text with Int/price as separator (original approach, now counts
#      EXACTLY 5 prices per category = 5 days)

CAT_HEADERS = {
    'Soup / Starter': 'Suppe', 'Soup/Starter': 'Suppe', 'Soup': 'Suppe',
    'Food 1': 'Essen 1', 'Essen 1': 'Essen 1', 'Gericht 1': 'Essen 1',
    'Food 2': 'Essen 2', 'Essen 2': 'Essen 2', 'Gericht 2': 'Essen 2',
    'Food 3': 'Essen 3', 'Essen 3': 'Essen 3', 'Gericht 3': 'Essen 3',
}
INT_RE  = re.compile(r'^Int$', re.I)
EXT_RE  = re.compile(r'^Ext$', re.I)
PRIC_RE = re.compile(r'^[\u20ac$]\d|^\d+[.,]\d{2}')
ALLG_RE = re.compile(r'^[A-H](\.[1-6])?$')
OR_RE   = re.compile(r'^(or|oder)$', re.I)

UI_NOISE = {
    'This website uses cookies to ensure you get the best experience on our website.',
    'Learn more', 'Got it!', 'Siemens | Menu', 'home', 'Home', 'view_compact',
    'Menu', 'place', 'Stores', 'Impressum', 'Nutzungsbedingungen',
    'Datenschutzerkl\u00e4rung', 'close', 'Close', 'English', 'menu',
    'Lunch', 'filter_list', 'Filter', 'Store', 'clear', 'Info',
    'New Webportal', '- Siemens Gastronomie', 'Register now:', 'MyCasinoCard',
    'Allergens and Additives',
    'Please check the allergens and additives during opening hours',
    'for further information', 'more information', 'List',
    'Description of all allergens and additives', 'Opening hours',
    'edit your personal profile', 'view transactions', 'QR Code for payment',
    'Use digital wallet to store QR Code', 'change card status',
    'other features', 'Go to MyCasinoCard',
}

def _lines_to_dishes(lines):
    """Parse a list of text lines into a list of dish dicts using Int/price."""
    dishes, cur = [], []
    i = 0
    while i < len(lines):
        tok = lines[i]
        if INT_RE.match(tok) and i + 1 < len(lines) and PRIC_RE.match(lines[i+1]):
            price = lines[i+1].lstrip('\u20ac$') + ' \u20ac'
            name_toks = [t for t in cur if not ALLG_RE.match(t) and not OR_RE.match(t)]
            name = ' '.join(name_toks).strip()
            if name and len(name) >= 3:
                low = name.lower()
                dishes.append({'name': name, 'preis_int': price,
                               'vv': 'VG' if 'vegan' in low else
                                    ('V' if 'vegetar' in low else '')})
            cur = []
            i += 2
            if i < len(lines) and EXT_RE.match(lines[i]):
                i += 1
                if i < len(lines) and PRIC_RE.match(lines[i]): i += 1
            continue
        if EXT_RE.match(tok):
            i += 1
            if i < len(lines) and PRIC_RE.match(lines[i]): i += 1
            continue
        if tok in CAT_HEADERS or tok in UI_NOISE:
            break
        if not ALLG_RE.match(tok):
            cur.append(tok)
        i += 1
    # flush last without price
    if cur:
        name_toks = [t for t in cur if not ALLG_RE.match(t) and not OR_RE.match(t)]
        name = ' '.join(name_toks).strip()
        if name and len(name) >= 3:
            low = name.lower()
            dishes.append({'name': name, 'preis_int': '',
                           'vv': 'VG' if 'vegan' in low else
                                ('V' if 'vegetar' in low else '')})
    return dishes

def _parse_dom_tabs(soup, local_dt):
    """
    Strategy A: find day-tab panels.
    The SPA uses <mat-tab-body> or role='tabpanel' or similar.
    Each panel has a date visible and its own category rows.
    """
    keys   = day_keys(local_dt)
    dates  = week_dates(local_dt)  # ['22.06', '23.06', ...]
    result = {k: [] for k in keys}

    # Try various tab-panel selectors
    panels = []
    for sel in [
        '[role="tabpanel"]',
        'mat-tab-body',
        '.mat-tab-body-content',
        '.tab-content',
        '.day-panel',
        '.menu-day',
    ]:
        found = soup.select(sel)
        if found:
            print(f"  [tabs] selector {sel!r} -> {len(found)} panels")
            panels = found
            break

    if not panels:
        return None  # fall through to flat parser

    # Match each panel to a weekday by finding its date text
    for panel in panels:
        txt = panel.get_text(' ', strip=True)
        matched_day = None
        for i, date in enumerate(dates):
            if date in txt:
                matched_day = keys[i]
                break
        if not matched_day:
            continue
        lines = [l.strip() for l in panel.get_text('\n').splitlines() if l.strip()]
        # Find category rows inside this panel
        ci_map = {}  # cat_label -> list of dish lines
        cur_cat, cur_lines2 = None, []
        for line in lines:
            if line in CAT_HEADERS:
                if cur_cat:
                    ci_map[cur_cat] = cur_lines2
                cur_cat, cur_lines2 = CAT_HEADERS[line], []
            elif cur_cat:
                cur_lines2.append(line)
        if cur_cat:
            ci_map[cur_cat] = cur_lines2
        for cat_label, raw in ci_map.items():
            dishes = _lines_to_dishes(raw)
            if dishes:
                result[matched_day].append({'kategorie': cat_label, **dishes[0]})

    filled = {k: v for k, v in result.items() if v}
    print(f"  [tabs] days with data: {list(filled.keys())}")
    return filled if len(filled) >= 3 else None

def _parse_dom_flat(soup, local_dt):
    """
    Strategy B: flat text.
    Each category block contains 5 dishes (one per day) separated by Int prices.
    We count exactly 5 Int-prices per category and assign them to days.
    """
    keys = day_keys(local_dt)
    for tag in soup(['script', 'style', 'noscript']): tag.decompose()
    lines = [l.strip() for l in soup.get_text('\n').splitlines() if l.strip()]

    menu_start = next((i for i, l in enumerate(lines) if l in CAT_HEADERS), None)
    if menu_start is None:
        print("  [flat] no category header found")
        for i, l in enumerate(lines[:80]):
            print(f"    {i:3d}: {l[:100]}")
        return {}
    print(f"  [flat] menu at line {menu_start}")

    # Build category blocks
    blocks = []
    cur_cat, cur_lines = None, []
    for line in lines[menu_start:]:
        if line in CAT_HEADERS:
            if cur_cat: blocks.append((cur_cat, cur_lines))
            cur_cat, cur_lines = CAT_HEADERS[line], []
        elif cur_cat:
            cur_lines.append(line)
    if cur_cat: blocks.append((cur_cat, cur_lines))
    print(f"  [flat] blocks: {[b[0] for b in blocks]}")

    week_data = {k: [] for k in keys}
    for cat_label, raw in blocks:
        # Count up to 5 dishes (one per day)
        # A dish ends when we see Int + price
        dishes, cur, day_idx = [], [], 0
        i = 0
        while i < len(raw) and day_idx < 5:
            tok = raw[i]
            if INT_RE.match(tok) and i+1 < len(raw) and PRIC_RE.match(raw[i+1]):
                price = raw[i+1].lstrip('\u20ac$') + ' \u20ac'
                name_toks = [t for t in cur if not ALLG_RE.match(t) and not OR_RE.match(t)]
                name = ' '.join(name_toks).strip()
                if name and len(name) >= 3:
                    low = name.lower()
                    week_data[keys[day_idx]].append({
                        'kategorie': cat_label, 'name': name,
                        'preis_int': price,
                        'vv': 'VG' if 'vegan' in low else ('V' if 'vegetar' in low else '')
                    })
                cur = []
                day_idx += 1
                i += 2
                if i < len(raw) and EXT_RE.match(raw[i]):
                    i += 1
                    if i < len(raw) and PRIC_RE.match(raw[i]): i += 1
                continue
            if EXT_RE.match(tok):
                i += 1
                if i < len(raw) and PRIC_RE.match(raw[i]): i += 1
                continue
            if tok in CAT_HEADERS or tok in UI_NOISE:
                break
            if not ALLG_RE.match(tok):
                cur.append(tok)
            i += 1
        print(f"  [flat] {cat_label}: {day_idx} days filled")
    return {k: v for k, v in week_data.items() if v}

def parse_dom(html: str, local_dt: datetime) -> dict:
    soup = BeautifulSoup(html, 'html.parser')

    # Try tab strategy first
    result = _parse_dom_tabs(soup, local_dt)
    if result:
        return result

    print("  [tabs] failed, using flat parser")
    return _parse_dom_flat(soup, local_dt)

# ── Render 800x600 JPEG ───────────────────────────────────────────────────────
def wrap_text(draw, text, f, max_w):
    words = text.split()
    out, cur = [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
        b = draw.textbbox((0, 0), t, font=f)
        if b[2] - b[0] <= max_w: cur = t
        else:
            if cur: out.append(cur)
            cur = w
    if cur: out.append(cur)
    return out

CATS = ['Suppe', 'Essen 1', 'Essen 2', 'Essen 3']

def render(week_data, kw, label, local_dt, url_menu, source=''):
    img = Image.new('RGB', (W, H), (255, 255, 255))
    d   = ImageDraw.Draw(img)
    ftit=lf(14,True); fday=lf(11,True); fcat=lf(8,True)
    ftxt=lf(9);       fbdg=lf(8,True);  fprc=lf(8); fftr=lf(9)
    HDR_H=36; LEGEND_H=17

    d.rectangle([(0,0),(W,HDR_H)], fill=BLUE)
    title = f'Siemens Kantine Regensburg  |  KW {kw:02d}'
    b = d.textbbox((0,0), title, font=ftit)
    d.text(((W-(b[2]-b[0]))//2,(HDR_H-(b[3]-b[1]))//2), title, font=ftit, fill=WHITE)
    y = HDR_H

    if not week_data:
        d.text((20,y+40), 'Speiseplan nicht verf\u00fcgbar.', font=ftxt, fill=C_TXT)
        d.text((20,y+60), url_menu, font=ftxt, fill=LIGHT)
        _footer(d,kw,label,local_dt,fftr,source); return img

    days = list(week_data.keys())[:5]
    dw   = W // len(days); DAY_H = 20
    for i, day in enumerate(days):
        x = i * dw
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)], fill=LIGHT)
        b = d.textbbox((0,0), day, font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2, y+(DAY_H-(b[3]-b[1]))//2), day, font=fday, fill=WHITE)
        if i > 0: d.line([(x,y),(x,y+DAY_H)], fill=BLUE, width=1)
    y += DAY_H

    avail = H - y - FOOTER_H - LEGEND_H - 2
    rs    = int(avail * 0.20)
    re_   = (avail - rs) // 3
    ROW_H = {'Suppe': rs, 'Essen 1': re_, 'Essen 2': re_, 'Essen 3': avail-rs-2*re_}

    for ri, cat in enumerate(CATS):
        rh = ROW_H[cat]
        d.rectangle([(0,y),(W,y+rh-1)], fill=R_ODD if ri%2==0 else R_EVEN)
        d.line([(0,y),(W,y)], fill=GRID, width=1)
        for i, day in enumerate(days):
            x = i * dw
            if i > 0: d.line([(x,y),(x,y+rh)], fill=GRID, width=1)
            items = [it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items:
                b = d.textbbox((0,0), '-', font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2, y+rh//2-6), '-', font=ftxt, fill=(180,180,180))
                continue
            it=items[0]; cx=x+4; cy=y+3; avw=dw-8
            d.text((cx,cy), it['kategorie'], font=fcat, fill=(100,100,100)); cy+=10
            if it['vv']:
                bl = 'Vegan' if it['vv']=='VG' else 'Veg.'
                bc = C_VG   if it['vv']=='VG' else C_V
                b  = d.textbbox((0,0), bl, font=fbdg)
                bw=b[2]-b[0]+5; bh=b[3]-b[1]+3
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh)], radius=3, fill=bc)
                d.text((cx+3,cy+1), bl, font=fbdg, fill=WHITE); cy+=bh+2
            for ln in wrap_text(d, it['name'], ftxt, avw)[:3]:
                d.text((cx,cy), ln, font=ftxt, fill=C_TXT); cy+=11
            if it['preis_int']:
                pl = f"Int: {it['preis_int']}"
                b  = d.textbbox((0,0), pl, font=fprc)
                d.text((x+dw-(b[2]-b[0])-3, y+rh-(b[3]-b[1])-3), pl, font=fprc, fill=LIGHT)
        y += rh

    d.line([(0,y),(W,y)], fill=GRID, width=1); y+=1
    d.rectangle([(0,y),(W,y+LEGEND_H)], fill=(245,249,253))
    d.rectangle([(5,y+4),(15,y+13)], fill=C_VG)
    d.text((19,y+3), 'Vegan',       font=fprc, fill=C_TXT)
    d.rectangle([(65,y+4),(75,y+13)], fill=C_V)
    d.text((79,y+3), 'Vegetarisch', font=fprc, fill=C_TXT)
    d.text((175,y+3), 'Int = Mitarbeiterpreis', font=fprc, fill=(120,120,120))
    _footer(d,kw,label,local_dt,fftr,source)
    return img

def _footer(d, kw, label, local_dt, f, source=''):
    src = f' \u2013 {source}' if source else ''
    txt = (f'KW {kw:02d} / {label}  \u2013  '
           f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  \u2013  "
           f"siemens.cateringportal.io{src}")
    d.rectangle([(0,H-FOOTER_H),(W,H)], fill=BLUE)
    b = d.textbbox((0,0), txt, font=f)
    d.text(((W-(b[2]-b[0]))//2, H-16), txt, font=f, fill=WHITE)

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    now   = datetime.now(timezone.utc)
    local = german_time(now)
    label, kw = kw_label(now)
    out_path  = OUT_DIR / f'kantine_{label}.jpg'

    monday   = local - timedelta(days=local.weekday())
    date_str = monday.strftime('%Y-%m-%d')
    url_menu = f"{URL_BASE}/date/{date_str}"
    if _SID: url_menu += f"?ste_sid={_SID}"

    print(f'Target URL : {url_menu}')
    print(f'Week label : {label}  (KW {kw:02d})')

    # 1. Try qnips API first (fastest, German PDF)
    print('Querying qnips API...')
    pdf_url = qnips_pdf_url(kw, local)

    # 2. Load page (always needed for DOM fallback; also intercepts PDF)
    print('Loading page...')
    html, intercepted = load_page(url_menu)
    if not pdf_url and intercepted:
        pdf_url = intercepted

    week_data = {}
    source    = ''

    # 3. Parse PDF (German)
    if pdf_url:
        print(f'PDF: {pdf_url[:100]}')
        try:
            pdf_bytes = download_pdf(pdf_url)
            pdf_text  = extract_pdf_text(pdf_bytes)
            week_data = parse_pdf(pdf_text, local)
            if week_data:
                source = 'PDF-DE'
                print(f'PDF ok: {list(week_data.keys())}')
        except Exception as e:
            print(f'PDF error: {e}')

    # 4. DOM fallback (English, tab+flat)
    if not week_data:
        print('DOM fallback...')
        week_data = parse_dom(html, local)
        if week_data: source = 'DOM-EN'
        print(f'DOM: {list(week_data.keys())}')

    img = render(week_data, kw, label, local, url_menu, source)
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})  source={source}')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')


if __name__ == '__main__':
    main()
