#!/usr/bin/env python3
"""Siemens Kantine Regensburg – DOM scraper + 800x600 landscape JPEG.

HTML line structure (SPA, English UI):
  ...noise...
  Mon. / 22.06. / Tue. / 23.06. / ... / Fri. / 26.06.
  Soup / Starter
    Today's soup / Int / €0.60 / Ext / €1.20
  Food 1
    Pancake / Vanilla sauce / Cherry compote / A / G / Int / €3.20 / Ext / €6.40
  Food 2
    Pizza with salami / G / Int / €6.10 / Ext / €12.20
    or
    Vegetarian pizza / Int / €5.80 / Ext / €11.60
    Fish / Paella Frutti di Mare / ... / Int / €5.90 / Ext / €11.80
    ...

Each category lists all 5 days sequentially.
An 'Int / € X.XX' pair terminates one day's entry within a category block.
Alternatives (line == 'or') are joined to the previous dish with ' / '.
"""
import os
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────
URL_BASE = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID = os.environ.get("CATERINGPORTAL_SID", "").strip()

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8
W, H     = 800, 600
FOOTER_H = 22

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

# ── Time ────────────────────────────────────────────────────────────────────
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
        return d - timedelta(days=(d.weekday()+1)%7)
    cs = last_sun(yr,3).replace(hour=1)
    ce = last_sun(yr,10).replace(hour=1)
    return dt + timedelta(hours=2 if cs<=dt<ce else 1)

def kw_label(dt):
    d = german_time(dt)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}", int(w)

# ── Step 1: Load page ─────────────────────────────────────────────────────────────
def load_page(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        page.goto(url, wait_until="load", timeout=60000)
        print("  Waiting for menu content...")
        try:
            page.wait_for_selector("text=Food 1", timeout=10000)
            print("  'Food 1' found")
        except Exception:
            try:
                page.wait_for_selector("text=Essen 1", timeout=5000)
                print("  'Essen 1' found")
            except Exception:
                print("  fallback timeout")
                page.wait_for_timeout(6000)
        page.wait_for_timeout(2000)
        print(f"  Title: {page.title()}")
        html = page.content()
        browser.close()
    print(f"  HTML size: {len(html):,} bytes")
    return html

# ── Step 2: Parse ─────────────────────────────────────────────────────────────────
# Recognised category header lines
CAT_HEADERS = {
    'Soup / Starter': 'Suppe',
    'Soup/Starter':   'Suppe',
    'Soup':           'Suppe',
    'Food 1':         'Essen 1',
    'Food 2':         'Essen 2',
    'Food 3':         'Essen 3',
    'Essen 1':        'Essen 1',
    'Essen 2':        'Essen 2',
    'Essen 3':        'Essen 3',
    'Gericht 1':      'Essen 1',
    'Gericht 2':      'Essen 2',
    'Gericht 3':      'Essen 3',
}

# Lines to drop entirely (navigation, cookies, etc.)
UI_NOISE = {
    'This website uses cookies to ensure you get the best experience on our website.',
    'Learn more', 'Got it!', 'Siemens | Menu', 'home', 'Home', 'view_compact',
    'Menu', 'place', 'Stores', 'Impressum', 'Nutzungsbedingungen',
    'Datenschutzerklärung', 'close', 'Close', 'English', 'menu', 'Lunch',
    'filter_list', 'Filter', 'Store', 'clear', 'Info', 'New Webportal',
    '- Siemens Gastronomie', 'Register now:', 'MyCasinoCard',
    'Allergens and Additives', 'Please check the allergens and additives during opening hours',
    'for further information', 'more information', 'List',
    'Description of all allergens and additives', 'Opening hours',
}

INT_RE  = re.compile(r'^Int$', re.I)
EXT_RE  = re.compile(r'^Ext$', re.I)
PRIC_RE = re.compile(r'^[€$]\d')        # €0.60  €12.20
DATE_RE = re.compile(r'^\d{2}\.\d{2}\.$') # 22.06.
DAY_RE  = re.compile(r'^(Mon|Tue|Wed|Thu|Fri|Mo|Di|Mi|Do|Fr)\.?$')
CW_RE   = re.compile(r'^CW:\s*\d+$')
ALLG_RE = re.compile(r'^[A-H](\.[1-6])?$') # single allergen token
OR_RE   = re.compile(r'^(or|oder)$', re.I)
VEGAN_RE= re.compile(r'\bvegan\b', re.I)
VEG_RE  = re.compile(r'\bvegetar', re.I)

def is_noise(line: str) -> bool:
    if line in UI_NOISE: return True
    if DATE_RE.match(line): return True
    if DAY_RE.match(line):  return True
    if CW_RE.match(line):   return True
    if INT_RE.match(line):  return True
    if EXT_RE.match(line):  return True
    if PRIC_RE.match(line): return True
    if ALLG_RE.match(line): return True
    if OR_RE.match(line):   return True
    if re.match(r'^Lunch \|', line): return True
    if re.match(r'^\d+$', line): return True
    return False

def detect_vv(tokens: list) -> str:
    txt = ' '.join(tokens)
    if VEGAN_RE.search(txt): return 'VG'
    if VEG_RE.search(txt):   return 'V'
    return ''

def extract_int_price(tokens: list) -> str:
    """
    Scan the token stream for  Int \u20acX.XX  and return 'X.XX €'.
    Tokens arrive as individual lines, so 'Int' and '€X.XX' are consecutive items.
    """
    for i, tok in enumerate(tokens):
        if INT_RE.match(tok) and i+1 < len(tokens) and PRIC_RE.match(tokens[i+1]):
            return tokens[i+1].lstrip('€$') + ' €'
    return ''

def parse_dom(html: str, local_dt: datetime) -> dict:
    monday    = local_dt - timedelta(days=local_dt.weekday())
    dates     = [(monday + timedelta(days=i)).strftime('%d.%m') for i in range(5)]
    day_short = ['Mo', 'Di', 'Mi', 'Do', 'Fr']
    day_keys  = [f"{day_short[i]} {dates[i]}" for i in range(5)]

    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript']): tag.decompose()
    lines = [l.strip() for l in soup.get_text('\n').splitlines() if l.strip()]

    print(f"  Total lines: {len(lines)}")

    # Find where the actual menu starts (after 'Soup / Starter' or first category)
    menu_start = None
    for i, line in enumerate(lines):
        if line in CAT_HEADERS:
            menu_start = i
            break
    if menu_start is None:
        print("  ERROR: no category header found")
        return {}
    print(f"  Menu starts at line {menu_start}: {lines[menu_start]!r}")

    # Split lines into category blocks
    # Each block: [ (cat_label, [lines...]), ... ]
    blocks = []   # list of (cat_label, raw_lines)
    cur_cat   = None
    cur_lines = []
    for line in lines[menu_start:]:
        if line in CAT_HEADERS:
            if cur_cat is not None:
                blocks.append((cur_cat, cur_lines))
            cur_cat   = CAT_HEADERS[line]
            cur_lines = []
        elif cur_cat is not None:
            cur_lines.append(line)
    if cur_cat:
        blocks.append((cur_cat, cur_lines))

    print(f"  Category blocks found: {[b[0] for b in blocks]}")

    # For each block, split into exactly 5 day-dishes.
    # Delimiter between days: the 'Int' token (followed by price).
    # We walk the lines; when we see 'Int' followed by '€X' we close the current dish.
    week_data = {k: [] for k in day_keys}

    for cat_label, raw in blocks:
        print(f"  Parsing {cat_label} ({len(raw)} raw lines): {raw[:20]}")
        dishes = []     # list of {name, price, vv}
        cur_toks = []   # name tokens for current day's dish
        i = 0
        while i < len(raw):
            tok = raw[i]
            # Int €X.XX -> close dish
            if INT_RE.match(tok) and i+1 < len(raw) and PRIC_RE.match(raw[i+1]):
                price = raw[i+1].lstrip('€$') + ' €'
                name_toks = [t for t in cur_toks if not ALLG_RE.match(t) and not OR_RE.match(t)]
                name = ' '.join(name_toks).strip()
                if name:
                    dishes.append({
                        'name':      name,
                        'preis_int': price,
                        'vv':        detect_vv(cur_toks),
                    })
                cur_toks = []
                i += 2  # skip €X.XX
                # also skip Ext €X.XX if next
                if i < len(raw) and EXT_RE.match(raw[i]):
                    i += 1
                    if i < len(raw) and PRIC_RE.match(raw[i]):
                        i += 1
                continue
            # Skip Ext price tokens
            if EXT_RE.match(tok):
                i += 1
                if i < len(raw) and PRIC_RE.match(raw[i]): i += 1
                continue
            # Stop at next category header or known end marker
            if tok in CAT_HEADERS or tok in UI_NOISE:
                break
            # Skip allergen tokens and 'or'/'oder' as content (keep rest)
            if not ALLG_RE.match(tok):
                cur_toks.append(tok)
            i += 1

        # flush last dish if no trailing Int price
        if cur_toks:
            name_toks = [t for t in cur_toks if not ALLG_RE.match(t) and not OR_RE.match(t)]
            name = ' '.join(name_toks).strip()
            if name:
                dishes.append({'name': name, 'preis_int': '', 'vv': detect_vv(cur_toks)})

        print(f"    -> {len(dishes)} dishes: {[(d['name'][:35], d['preis_int']) for d in dishes]}")

        # Assign dishes to days (index 0=Mo, 1=Di, ...)
        for di, day_key in enumerate(day_keys):
            if di < len(dishes):
                week_data[day_key].append({
                    'kategorie': cat_label,
                    **dishes[di],
                })

    return {k: v for k, v in week_data.items() if v}

# ── Render 800x600 landscape JPEG ───────────────────────────────────────────────────
def wrap_text(draw, text, f, max_w):
    words = text.split()
    out, cur = [], ''
    for w in words:
        t = (cur+' '+w).strip()
        b = draw.textbbox((0,0), t, font=f)
        if b[2]-b[0] <= max_w: cur = t
        else:
            if cur: out.append(cur)
            cur = w
    if cur: out.append(cur)
    return out

CATS = ['Suppe', 'Essen 1', 'Essen 2', 'Essen 3']

def render(week_data, kw, label, local_dt, url_menu):
    img = Image.new('RGB', (W,H), (255,255,255))
    d   = ImageDraw.Draw(img)
    ftit=lf(14,True); fday=lf(11,True); fcat=lf(8,True)
    ftxt=lf(9);       fbdg=lf(8,True);  fprc=lf(8); fftr=lf(9)
    HDR_H=36; LEGEND_H=17

    # Header
    d.rectangle([(0,0),(W,HDR_H)], fill=BLUE)
    title = f'Siemens Kantine Regensburg  |  KW {kw:02d}'
    b = d.textbbox((0,0), title, font=ftit)
    d.text(((W-(b[2]-b[0]))//2, (HDR_H-(b[3]-b[1]))//2), title, font=ftit, fill=WHITE)
    y = HDR_H

    if not week_data:
        d.text((20,y+40), 'Speiseplan konnte nicht geladen werden.', font=ftxt, fill=C_TXT)
        d.text((20,y+60), 'Bitte manuell prüfen:', font=ftxt, fill=C_TXT)
        d.text((20,y+80), url_menu, font=ftxt, fill=LIGHT)
        _footer(d, kw, label, local_dt, fftr)
        return img

    days = list(week_data.keys())[:5]
    dw   = W // len(days)
    DAY_H = 20
    for i, day in enumerate(days):
        x = i*dw
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)], fill=LIGHT)
        b = d.textbbox((0,0), day, font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2, y+(DAY_H-(b[3]-b[1]))//2), day, font=fday, fill=WHITE)
        if i>0: d.line([(x,y),(x,y+DAY_H)], fill=BLUE, width=1)
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
            x = i*dw
            if i>0: d.line([(x,y),(x,y+rh)], fill=GRID, width=1)
            items = [it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items:
                b = d.textbbox((0,0), '-', font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2, y+rh//2-6), '-', font=ftxt, fill=(180,180,180))
                continue
            it = items[0]; cx = x+4; cy = y+3; avw = dw-8
            d.text((cx,cy), it['kategorie'], font=fcat, fill=(100,100,100)); cy += 10
            if it['vv']:
                bl = 'Vegan' if it['vv']=='VG' else 'Veg.'
                bc = C_VG   if it['vv']=='VG' else C_V
                b  = d.textbbox((0,0), bl, font=fbdg)
                bw = b[2]-b[0]+5; bh = b[3]-b[1]+3
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh)], radius=3, fill=bc)
                d.text((cx+3,cy+1), bl, font=fbdg, fill=WHITE); cy += bh+2
            for ln in wrap_text(d, it['name'], ftxt, avw)[:3]:
                d.text((cx,cy), ln, font=ftxt, fill=C_TXT); cy += 11
            if it['preis_int']:
                pl = f"Int: {it['preis_int']}"
                b  = d.textbbox((0,0), pl, font=fprc)
                d.text((x+dw-(b[2]-b[0])-3, y+rh-(b[3]-b[1])-3), pl, font=fprc, fill=LIGHT)
        y += rh

    d.line([(0,y),(W,y)], fill=GRID, width=1); y += 1
    d.rectangle([(0,y),(W,y+LEGEND_H)], fill=(245,249,253))
    d.rectangle([( 5,y+4),(15,y+13)], fill=C_VG)
    d.text((19,y+3), 'Vegan',        font=fprc, fill=C_TXT)
    d.rectangle([(65,y+4),(75,y+13)], fill=C_V)
    d.text((79,y+3), 'Vegetarisch',  font=fprc, fill=C_TXT)
    d.text((175,y+3),'Int = Mitarbeiterpreis', font=fprc, fill=(120,120,120))
    _footer(d, kw, label, local_dt, fftr)
    return img

def _footer(d, kw, label, local_dt, f):
    txt = (f'KW {kw:02d} / {label}  –  '
           f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  –  siemens.cateringportal.io")
    d.rectangle([(0,H-FOOTER_H),(W,H)], fill=BLUE)
    b = d.textbbox((0,0), txt, font=f)
    d.text(((W-(b[2]-b[0]))//2, H-16), txt, font=f, fill=WHITE)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now   = datetime.now(timezone.utc)
    local = german_time(now)
    label, kw = kw_label(now)
    out_path  = OUT_DIR / f'kantine_{label}.jpg'

    date_str = local.strftime('%Y-%m-%d')
    url_menu = f"{URL_BASE}/date/{date_str}"
    if _SID: url_menu += f"?ste_sid={_SID}"

    print(f'Target URL : {url_menu}')
    print(f'Week label : {label}  (KW {kw:02d})')

    html = load_page(url_menu)

    print('Parsing DOM...')
    week_data = parse_dom(html, local)
    print(f'Days parsed: {list(week_data.keys())}')

    img = render(week_data, kw, label, local, url_menu)
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')


if __name__ == '__main__':
    main()
