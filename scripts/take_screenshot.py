#!/usr/bin/env python3
"""Siemens Kantine Regensburg – DOM-based menu scraper + 800x600 JPEG renderer.

The menu data is rendered directly in the HTML by the SPA.
After waiting for JS, BeautifulSoup extracts the visible text which contains:
  Mon. 22.06. | Tue. 23.06. | ... dates ...
  Soup/Starter | Food 1 | Food 2 | Food 3
  dish name, allergen codes, Int €X.XX Ext €Y.YY
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

# ── Fonts ───────────────────────────────────────────────────────────────────
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

# ── Step 2: Parse DOM ───────────────────────────────────────────────────────────────
CAT_PATTERNS = [
    (r'Soup\s*/\s*Starter|Suppe',       'Suppe'),
    (r'Food\s*1|Essen\s*1|Gericht\s*1', 'Essen 1'),
    (r'Food\s*2|Essen\s*2|Gericht\s*2', 'Essen 2'),
    (r'Food\s*3|Essen\s*3|Gericht\s*3', 'Essen 3'),
]

ALLERGEN_RE = re.compile(
    r'\s+([A-H](?:\.[1-6])?(?:\s+[A-H](?:\.[1-6])?)*)\s*$'
)
PRICE_INT_RE = re.compile(r'Int\s*\u20ac?\s*(\d+[,.]\d{2})', re.I)
PRICE_EXT_RE = re.compile(r'Ext\s*\u20ac?\s*(\d+[,.]\d{2})', re.I)
VEGAN_RE     = re.compile(r'\bvegan\b', re.I)
VEG_RE       = re.compile(r'\bvegetar', re.I)

NOISE_WORDS = {
    'int','ext','cw:','home','menu','stores','impressum','nutzungsbedingungen',
    'datenschutzerklärung','close','english','lunch','this','website','uses',
    'cookies','learn','more','got','it!','siemens','view_compact','place',
    'menu','home','soup','starter','food','essen','gericht','suppe',
}

def detect_vv(text):
    if VEGAN_RE.search(text): return 'VG'
    if VEG_RE.search(text):   return 'V'
    return ''

def strip_allergens(name):
    return ALLERGEN_RE.sub('', name).strip()

def parse_dom(html: str, local_dt: datetime) -> dict:
    monday    = local_dt - timedelta(days=local_dt.weekday())
    dates     = [(monday + timedelta(days=i)).strftime('%d.%m') for i in range(5)]
    day_short = ['Mo','Di','Mi','Do','Fr']

    soup = BeautifulSoup(html, 'html.parser')

    # --- Strategy A: find day-column containers by date string ---
    day_cols = {}
    for i, date in enumerate(dates):
        for el in soup.find_all(string=re.compile(re.escape(date))):
            parent = el.find_parent(['td','th','div','section','article','li'])
            if parent and date not in str(day_cols):
                day_key = f"{day_short[i]} {date}"
                day_cols[day_key] = parent
                print(f"  Day col [{day_key}]: <{parent.name} class={parent.get('class','')}>"),
                break

    if len(day_cols) >= 3:
        print(f"  Strategy A: {len(day_cols)} columns found")
        return _parse_columns(day_cols)

    # --- Strategy B: line-by-line flat text ---
    print("  Strategy B: flat text")
    return _parse_flat(soup, dates, day_short)


def _parse_columns(day_cols: dict) -> dict:
    week_data = {}
    for day_key, col_el in day_cols.items():
        col_text = col_el.get_text(' ', strip=True)
        print(f"  [{day_key}] text: {col_text[:180]}")
        meals = []
        for cat_re, cat_label in CAT_PATTERNS:
            m = re.search(cat_re, col_text, re.I)
            if not m: continue
            rest = col_text[m.end():]
            # Cut before next category
            nc = re.search(r'(?:Food\s*[123]|Essen\s*[123]|Soup|Suppe)', rest, re.I)
            seg = rest[:nc.start()] if nc else rest[:300]
            # Extract int price
            pm = PRICE_INT_RE.search(seg)
            price = pm.group(1) + ' €' if pm else ''
            # Name: everything before price / allergen block
            name_raw = seg[:pm.start()] if pm else seg
            name = strip_allergens(
                re.sub(r'\s+', ' ', re.sub(r'Int\s*€.*', '', name_raw)).strip()
            )
            if name and len(name) >= 3:
                meals.append({
                    'kategorie': cat_label, 'name': name,
                    'vv': detect_vv(seg), 'preis_int': price,
                })
        if meals:
            week_data[day_key] = meals
    return week_data


def _parse_flat(soup, dates, day_short) -> dict:
    for tag in soup(['script','style','noscript']): tag.decompose()
    lines = [l.strip() for l in soup.get_text('\n').splitlines() if l.strip()]
    print(f"  Lines total: {len(lines)}")
    print("  Lines 0-80:")
    for i, l in enumerate(lines[:80]):
        print(f"    {i:3d}: {l[:100]}")

    # Locate date lines
    day_idx = {}
    for i, date in enumerate(dates):
        for li, line in enumerate(lines):
            if date in line:
                day_idx[i] = li
                print(f"  Date {date} at line {li}: {line!r}")
                break

    if not day_idx:
        print("  No dates found!")
        return {}

    # Locate category lines
    cat_idx = {}
    after = min(day_idx.values()) + 3
    for li in range(after, len(lines)):
        for cat_re, cat_label in CAT_PATTERNS:
            if re.search(cat_re, lines[li], re.I) and cat_label not in cat_idx:
                cat_idx[cat_label] = li
                print(f"  Cat '{cat_label}' at line {li}: {lines[li]!r}")

    print(f"  day_idx={day_idx}  cat_idx={cat_idx}")

    week_data = {f"{day_short[i]} {dates[i]}": [] for i in range(5)}
    cats_sorted = sorted(cat_idx.items(), key=lambda x: x[1])

    for ci, (cat_label, cat_li) in enumerate(cats_sorted):
        end_li = cats_sorted[ci+1][1] if ci+1 < len(cats_sorted) else min(cat_li+60, len(lines))
        block  = lines[cat_li+1:end_li]
        print(f"  {cat_label} block ({len(block)} lines): {block[:15]}")

        # Collect dish groups separated by Int € price tokens
        dishes = []; cur = []; cur_price = ''
        for tok in block:
            pm = PRICE_INT_RE.search(tok)
            if pm:
                cur_price = pm.group(1) + ' €'
                name = strip_allergens(' '.join(cur))
                if name and len(name) >= 3:
                    dishes.append((name, cur_price, detect_vv(' '.join(cur))))
                cur = []; cur_price = ''
            elif PRICE_EXT_RE.match(tok):
                pass  # skip ext price
            elif tok.lower() not in NOISE_WORDS and not re.match(r'^[€\d,. ]+$', tok):
                cur.append(tok)
        if cur:
            name = strip_allergens(' '.join(cur))
            if name and len(name) >= 3:
                dishes.append((name, cur_price, detect_vv(' '.join(cur))))

        print(f"  {cat_label}: {[(n[:40],p) for n,p,_ in dishes]}")

        for di in range(5):
            if di < len(dishes):
                name, price, vv = dishes[di]
                week_data[f"{day_short[di]} {dates[di]}"].append({
                    'kategorie': cat_label, 'name': name,
                    'vv': vv, 'preis_int': price,
                })

    return {k: v for k, v in week_data.items() if v}

# ── Render 800x600 JPEG ──────────────────────────────────────────────────────────────
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

CATS = ['Suppe','Essen 1','Essen 2','Essen 3']

def render(week_data, kw, label, local_dt, url_menu):
    img = Image.new('RGB',(W,H),(255,255,255))
    d   = ImageDraw.Draw(img)
    ftit=lf(14,True); fday=lf(11,True); fcat=lf(8,True)
    ftxt=lf(9); fbdg=lf(8,True); fprc=lf(8); fftr=lf(9)
    HDR_H=36; LEGEND_H=17

    d.rectangle([(0,0),(W,HDR_H)],fill=BLUE)
    title=f'Siemens Kantine Regensburg  |  KW {kw:02d}'
    b=d.textbbox((0,0),title,font=ftit)
    d.text(((W-(b[2]-b[0]))//2,(HDR_H-(b[3]-b[1]))//2),title,font=ftit,fill=WHITE)
    y=HDR_H

    if not week_data:
        d.text((20,y+40),'Speiseplan konnte nicht geladen werden.',font=ftxt,fill=C_TXT)
        d.text((20,y+60),'Bitte manuell pr\u00fcfen:',font=ftxt,fill=C_TXT)
        d.text((20,y+80),url_menu,font=ftxt,fill=LIGHT)
        _footer(d,kw,label,local_dt,fftr); return img

    days=list(week_data.keys())[:5]
    dw=W//len(days); DAY_H=20
    for i,day in enumerate(days):
        x=i*dw
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)],fill=LIGHT)
        b=d.textbbox((0,0),day,font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2,y+(DAY_H-(b[3]-b[1]))//2),day,font=fday,fill=WHITE)
        if i>0: d.line([(x,y),(x,y+DAY_H)],fill=BLUE,width=1)
    y+=DAY_H

    avail=H-y-FOOTER_H-LEGEND_H-2
    rs=int(avail*0.20); re_=(avail-rs)//3
    ROW_H={'Suppe':rs,'Essen 1':re_,'Essen 2':re_,'Essen 3':avail-rs-2*re_}

    for ri,cat in enumerate(CATS):
        rh=ROW_H[cat]
        d.rectangle([(0,y),(W,y+rh-1)],fill=R_ODD if ri%2==0 else R_EVEN)
        d.line([(0,y),(W,y)],fill=GRID,width=1)
        for i,day in enumerate(days):
            x=i*dw
            if i>0: d.line([(x,y),(x,y+rh)],fill=GRID,width=1)
            items=[it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items:
                b=d.textbbox((0,0),'-',font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-6),'-',font=ftxt,fill=(180,180,180))
                continue
            it=items[0]; cx=x+4; cy=y+3; avw=dw-8
            d.text((cx,cy),it['kategorie'],font=fcat,fill=(100,100,100)); cy+=10
            if it['vv']:
                bl='Vegan' if it['vv']=='VG' else 'Veg.'
                bc=C_VG if it['vv']=='VG' else C_V
                b=d.textbbox((0,0),bl,font=fbdg)
                bw=b[2]-b[0]+5; bh=b[3]-b[1]+3
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh)],radius=3,fill=bc)
                d.text((cx+3,cy+1),bl,font=fbdg,fill=WHITE); cy+=bh+2
            for ln in wrap_text(d,it['name'],ftxt,avw)[:3]:
                d.text((cx,cy),ln,font=ftxt,fill=C_TXT); cy+=11
            if it['preis_int']:
                pl=f"Int: {it['preis_int']}"
                b=d.textbbox((0,0),pl,font=fprc)
                d.text((x+dw-(b[2]-b[0])-3,y+rh-(b[3]-b[1])-3),pl,font=fprc,fill=LIGHT)
        y+=rh

    d.line([(0,y),(W,y)],fill=GRID,width=1); y+=1
    d.rectangle([(0,y),(W,y+LEGEND_H)],fill=(245,249,253))
    d.rectangle([( 5,y+4),(15,y+13)],fill=C_VG)
    d.text((19,y+3),'Vegan',font=fprc,fill=C_TXT)
    d.rectangle([(65,y+4),(75,y+13)],fill=C_V)
    d.text((79,y+3),'Vegetarisch',font=fprc,fill=C_TXT)
    d.text((175,y+3),'Int = Mitarbeiterpreis',font=fprc,fill=(120,120,120))
    _footer(d,kw,label,local_dt,fftr)
    return img

def _footer(d,kw,label,local_dt,f):
    txt=(f'KW {kw:02d} / {label}  \u2013  '
         f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  \u2013  siemens.cateringportal.io")
    d.rectangle([(0,H-FOOTER_H),(W,H)],fill=BLUE)
    b=d.textbbox((0,0),txt,font=f)
    d.text(((W-(b[2]-b[0]))//2,H-16),txt,font=f,fill=WHITE)

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
