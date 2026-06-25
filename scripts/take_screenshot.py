#!/usr/bin/env python3
"""Siemens Kantine Regensburg – menu scraper + 800x600 landscape JPEG.

Strategy (in order):
  1. Load page with Playwright, intercept any qnips PDF response body
  2. Try to find a Mittagessen_DE_<KW>_*.pdf link in the HTML
  3. Try to construct the qnips PDF URL from known pattern
  4. If a PDF is found: parse it (German text, column layout)
  5. Fallback: parse DOM text (English) and translate dish names to German
"""
import io
import os
import re
import requests
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

# ── Step 1: Load page + intercept PDF links ───────────────────────────────────
def load_page(url: str):
    """Returns (html, pdf_url_or_None)"""
    found_pdf = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        def on_response(resp):
            u = resp.url
            if 'qnips' in u and '.pdf' in u.lower() and 'allergen' not in u.lower():
                print(f"  [network PDF] {u[:120]}")
                found_pdf.append(u)
            elif 'qnips' in u:
                ct = resp.headers.get('content-type', '')
                if 'json' in ct or 'text' in ct:
                    try:
                        body = resp.text()
                        urls = re.findall(
                            r'https://files\.qnips\.com/[^\s"<>\']+\.pdf[^\s"<>\']*',
                            body, re.I)
                        if urls:
                            print(f"  [json PDF] {urls[0][:120]}")
                            found_pdf.extend(urls)
                    except Exception:
                        pass
        page.on("response", on_response)
        page.goto(url, wait_until="load", timeout=60000)
        print("  Waiting for menu content...")
        try:
            page.wait_for_selector("text=Food 1", timeout=10000)
            print("  'Food 1' found")
        except Exception:
            page.wait_for_timeout(6000)
        page.wait_for_timeout(3000)
        print(f"  Title: {page.title()}")
        html = page.content()
        browser.close()
    print(f"  HTML: {len(html):,} bytes  |  intercepted PDFs: {found_pdf}")
    # Also search raw HTML for qnips PDF
    html_pdfs = re.findall(
        r'https://files\.qnips\.com/[^\s"<>\']+Mittagessen[^\s"<>\']+\.pdf[^\s"<>\']*',
        html, re.I)
    if html_pdfs:
        print(f"  [html PDF] {html_pdfs[0][:120]}")
        found_pdf.extend(html_pdfs)
    return html, found_pdf[0] if found_pdf else None

# ── Step 2: Try to build/guess qnips PDF URL ──────────────────────────────────
def try_construct_pdf_url(kw: int, local_dt: datetime) -> str | None:
    """
    qnips PDF URL pattern:
      https://files.qnips.com/release-menu-pdfs/Mittagessen_DE_<KW>_<YYYYMMDD>_<ID>.pdf
    We don’t know the ID, but we can try the qnips API directly.
    """
    # Try the qnips store API to get the PDF list
    # Store ID 41196 is used by the SPA (seen in Surveys API call)
    store_id = "41196"
    monday   = local_dt - timedelta(days=local_dt.weekday())
    date_str = monday.strftime("%Y-%m-%d")
    api_urls = [
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{store_id}/MenuPdfs?date={date_str}",
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{store_id}/Menus?date={date_str}",
        f"https://apps-live-eu.qnips.com/cons/api/Stores/{store_id}/Menuplans?date={date_str}",
    ]
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for api_url in api_urls:
        try:
            r = requests.get(api_url, headers=headers, timeout=10)
            if r.status_code == 200:
                body = r.text
                urls = re.findall(
                    r'https://files\.qnips\.com/[^\s"<>\']+\.pdf[^\s"<>\']*',
                    body, re.I)
                if urls:
                    print(f"  [API] Found PDF via {api_url}: {urls[0][:100]}")
                    return urls[0]
                print(f"  [API] {api_url} -> {r.status_code}, no PDF URL in body")
        except Exception as e:
            print(f"  [API] {api_url} -> error: {e}")
    return None

# ── Step 3: Download + parse PDF (German) ─────────────────────────────────────
def download_pdf(url: str) -> bytes:
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    print(f"  PDF: {len(r.content):,} bytes")
    return r.content

def extract_pdf_text(pdf_bytes: bytes) -> str:
    if pdfplumber:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
            if text.strip():
                print(f"  pdfplumber: {len(text)} chars")
                return text
        except Exception as e:
            print(f"  pdfplumber error: {e}")
    if PdfReader:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = '\n'.join(p.extract_text() or '' for p in reader.pages)
        print(f"  pypdf: {len(text)} chars")
        return text
    raise RuntimeError("No PDF parser")

def dedup(text):
    for _ in range(6):
        text = re.sub(r'([A-Za-z\u00c0-\u017e,\- ]{4,})\1', r'\1', text)
    return text

def fix_name(name):
    name = re.sub(r'([a-z\u00e0-\u017e])([A-Z\u00c0-\u00de])', r'\1 \2', name)
    return re.sub(r'\s+', ' ', name).strip()

def parse_pdf(pdf_text: str, local_dt: datetime) -> dict:
    """Parse qnips column layout: 5 items per category = 1 per day."""
    monday    = local_dt - timedelta(days=local_dt.weekday())
    dates     = [(monday+timedelta(days=i)).strftime('%d.%m') for i in range(5)]
    day_short = ['Mo','Di','Mi','Do','Fr']
    day_keys  = [f"{day_short[i]} {dates[i]}" for i in range(5)]

    text = ' '.join(pdf_text.split())
    text = dedup(text)
    text = re.sub(
        r'^.*?(?:Restaurant\s+Regensburg|\+49[^\s]+|\d{2}:\d{2}\s+Uhr)\s*',
        '', text, flags=re.I).strip()

    # Split at price pairs  "3,206,40" = int+ext merged
    parts = re.split(r'(?<=\d{2})  +(?=[A-Z\u00c0-\u00de])', text)
    items = []
    for part in parts:
        part = part.strip()
        if not part: continue
        m = re.search(r'(\d+,\d{2})(\d+,\d{2})\s*$', part)
        if not m:
            m = re.search(r'(\d+,\d{2})\s+(\d+,\d{2})\s*$', part)
        if m:
            raw_name = part[:m.start()].strip()
            price    = m.group(1) + ' €'
        else:
            raw_name = part
            price    = ''
        name = fix_name(raw_name)
        if not name or len(name) < 3: continue
        low = name.lower()
        if any(low.startswith(w) for w in
               ['oder','int.ext','mo - fr','alle preise','all prices',
                'restaurant','thomas','+49','allergen']): continue
        if re.match(r'^[\d.,\s€/]+$', name): continue
        vv = 'VG' if 'vegan' in low else ('V' if 'vegetar' in low else '')
        items.append({'name': name, 'preis_int': price, 'vv': vv})

    print(f"  PDF items: {len(items)}")
    for i, it in enumerate(items):
        print(f"    {i:2d}: {it['name'][:55]!r:60} {it['preis_int']}")

    CATS = ['Suppe','Essen 1','Essen 2','Essen 3']
    week_data = {k: [] for k in day_keys}
    for ci, cat in enumerate(CATS):
        for di, day_key in enumerate(day_keys):
            idx = ci*5+di
            if idx < len(items):
                week_data[day_key].append({'kategorie': cat, **items[idx]})
    return {k: v for k, v in week_data.items() if v}

# ── Step 4: Parse DOM (English) as fallback ──────────────────────────────────
CAT_HEADERS = {
    'Soup / Starter': 'Suppe',  'Soup/Starter': 'Suppe',  'Soup': 'Suppe',
    'Food 1': 'Essen 1',        'Essen 1': 'Essen 1',     'Gericht 1': 'Essen 1',
    'Food 2': 'Essen 2',        'Essen 2': 'Essen 2',     'Gericht 2': 'Essen 2',
    'Food 3': 'Essen 3',        'Essen 3': 'Essen 3',     'Gericht 3': 'Essen 3',
}
INT_RE   = re.compile(r'^Int$', re.I)
EXT_RE   = re.compile(r'^Ext$', re.I)
PRIC_RE  = re.compile(r'^[€$\d]\d*[.,]\d')  # €0.60 or 0.60
ALLG_RE  = re.compile(r'^[A-H](\.[1-6])?$')
OR_RE    = re.compile(r'^(or|oder)$', re.I)
VEGAN_RE = re.compile(r'\bvegan\b', re.I)
VEG_RE   = re.compile(r'\bvegetar', re.I)

UI_NOISE = {
    'This website uses cookies to ensure you get the best experience on our website.',
    'Learn more','Got it!','Siemens | Menu','home','Home','view_compact','Menu',
    'place','Stores','Impressum','Nutzungsbedingungen','Datenschutzerklärung',
    'close','Close','English','menu','Lunch','filter_list','Filter','Store',
    'clear','Info','New Webportal','- Siemens Gastronomie','Register now:',
    'MyCasinoCard','Allergens and Additives',
    'Please check the allergens and additives during opening hours',
    'for further information','more information','List',
    'Description of all allergens and additives','Opening hours',
    'edit your personal profile','view transactions','QR Code for payment',
    'Use digital wallet to store QR Code','change card status','other features',
    'Go to MyCasinoCard',
}

def parse_dom(html: str, local_dt: datetime) -> dict:
    monday    = local_dt - timedelta(days=local_dt.weekday())
    dates     = [(monday+timedelta(days=i)).strftime('%d.%m') for i in range(5)]
    day_short = ['Mo','Di','Mi','Do','Fr']
    day_keys  = [f"{day_short[i]} {dates[i]}" for i in range(5)]

    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script','style','noscript']): tag.decompose()
    lines = [l.strip() for l in soup.get_text('\n').splitlines() if l.strip()]

    # Find first category header
    menu_start = next((i for i,l in enumerate(lines) if l in CAT_HEADERS), None)
    if menu_start is None:
        print("  ERROR: no category header in DOM")
        return {}
    print(f"  Menu starts at line {menu_start}")

    # Split into category blocks
    blocks = []
    cur_cat, cur_lines = None, []
    for line in lines[menu_start:]:
        if line in CAT_HEADERS:
            if cur_cat: blocks.append((cur_cat, cur_lines))
            cur_cat, cur_lines = CAT_HEADERS[line], []
        elif cur_cat:
            cur_lines.append(line)
    if cur_cat: blocks.append((cur_cat, cur_lines))

    print(f"  Blocks: {[b[0] for b in blocks]}")

    week_data = {k: [] for k in day_keys}
    for cat_label, raw in blocks:
        dishes = []
        cur_toks = []
        i = 0
        while i < len(raw):
            tok = raw[i]
            if INT_RE.match(tok) and i+1 < len(raw) and PRIC_RE.match(raw[i+1]):
                price = raw[i+1].lstrip('€$') + ' €'
                name_toks = [t for t in cur_toks if not ALLG_RE.match(t) and not OR_RE.match(t)]
                name = ' '.join(name_toks).strip()
                if name and len(name) >= 3:
                    dishes.append({'name': name, 'preis_int': price,
                                   'vv': 'VG' if VEGAN_RE.search(name) else
                                         ('V' if VEG_RE.search(name) else '')})
                cur_toks = []
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
                cur_toks.append(tok)
            i += 1
        if cur_toks:
            name_toks = [t for t in cur_toks if not ALLG_RE.match(t) and not OR_RE.match(t)]
            name = ' '.join(name_toks).strip()
            if name and len(name) >= 3:
                dishes.append({'name': name, 'preis_int': '',
                               'vv': 'VG' if VEGAN_RE.search(name) else
                                     ('V' if VEG_RE.search(name) else '')})
        print(f"  {cat_label}: {len(dishes)} dishes")
        for di, day_key in enumerate(day_keys):
            if di < len(dishes):
                week_data[day_key].append({'kategorie': cat_label, **dishes[di]})
    return {k: v for k, v in week_data.items() if v}

# ── Render ─────────────────────────────────────────────────────────────────────
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

def render(week_data, kw, label, local_dt, url_menu, source=''):
    img = Image.new('RGB',(W,H),(255,255,255))
    d   = ImageDraw.Draw(img)
    ftit=lf(14,True); fday=lf(11,True); fcat=lf(8,True)
    ftxt=lf(9);       fbdg=lf(8,True);  fprc=lf(8); fftr=lf(9)
    HDR_H=36; LEGEND_H=17

    d.rectangle([(0,0),(W,HDR_H)],fill=BLUE)
    title=f'Siemens Kantine Regensburg  |  KW {kw:02d}'
    b=d.textbbox((0,0),title,font=ftit)
    d.text(((W-(b[2]-b[0]))//2,(HDR_H-(b[3]-b[1]))//2),title,font=ftit,fill=WHITE)
    y=HDR_H

    if not week_data:
        d.text((20,y+40),'Speiseplan konnte nicht geladen werden.',font=ftxt,fill=C_TXT)
        d.text((20,y+60),'Bitte manuell prüfen:',font=ftxt,fill=C_TXT)
        d.text((20,y+80),url_menu,font=ftxt,fill=LIGHT)
        _footer(d,kw,label,local_dt,fftr,source); return img

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
    _footer(d,kw,label,local_dt,fftr,source)
    return img

def _footer(d,kw,label,local_dt,f,source=''):
    src = f' – {source}' if source else ''
    txt=(f'KW {kw:02d} / {label}  –  '
         f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  –  siemens.cateringportal.io{src}")
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

    # 1. Load page
    print('Loading page...')
    html, pdf_url = load_page(url_menu)

    # 2. If no PDF intercepted, try qnips API
    if not pdf_url:
        print('No PDF intercepted, trying qnips API...')
        pdf_url = try_construct_pdf_url(kw, local)

    week_data = {}
    source    = ''

    # 3. Try PDF (German)
    if pdf_url:
        print(f'Downloading PDF: {pdf_url[:100]}')
        try:
            pdf_bytes = download_pdf(pdf_url)
            pdf_text  = extract_pdf_text(pdf_bytes)
            print('Parsing PDF (German)...')
            week_data = parse_pdf(pdf_text, local)
            if week_data:
                source = 'PDF'
                print(f'PDF days parsed: {list(week_data.keys())}')
        except Exception as e:
            print(f'PDF failed: {e}')

    # 4. Fallback: DOM
    if not week_data:
        print('Fallback: parsing DOM (English)...')
        week_data = parse_dom(html, local)
        if week_data:
            source = 'DOM'
        print(f'DOM days parsed: {list(week_data.keys())}')

    # 5. Render
    img = render(week_data, kw, label, local, url_menu, source)
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})  source={source}')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')


if __name__ == '__main__':
    main()
