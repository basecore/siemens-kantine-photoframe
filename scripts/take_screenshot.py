#!/usr/bin/env python3
"""Siemens Kantine Regensburg – weekly menu PDF scraper + 800x600 JPEG renderer.

Strategy:
  1. Open cateringportal.io with Playwright
  2. Intercept ALL network responses to catch the qnips PDF URL
     (the link is not in the HTML but loaded dynamically via JS/API)
  3. Download the weekly PDF with requests
  4. Parse menu items from PDF text (dedup + price-pair splitting)
  5. Render clean 800x600 LANDSCAPE JPEG with Pillow
"""
import os
import re
import io
import requests
from pathlib import Path
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

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
URL_BASE = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID = os.environ.get("CATERINGPORTAL_SID", "").strip()

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8
W, H = 800, 600
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

# ── Font helper ───────────────────────────────────────────────────────────────
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

# ── Time helpers ─────────────────────────────────────────────────────────────
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

# ── Step 1: Load page, intercept ALL responses for qnips PDF ────────────────────
def load_page(url: str) -> tuple:
    """
    Returns (html, pdf_url_or_None).
    Intercepts every network response URL to catch qnips PDF links
    that are set dynamically via JS/API and never appear in the HTML source.
    """
    found_pdf = []
    found_api_json = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        page = context.new_page()

        # Intercept every request URL
        def on_request(req):
            u = req.url
            if 'qnips' in u.lower():
                print(f"  [req] qnips: {u[:140]}")
            if '.pdf' in u.lower() and 'allergen' not in u.lower():
                print(f"  [req] PDF: {u[:140]}")
                found_pdf.append(u)

        # Intercept every response to also catch JSON API payloads
        def on_response(resp):
            u = resp.url
            if 'qnips' in u.lower():
                print(f"  [resp] qnips: {u[:140]}")
                # Try to extract PDF URL from JSON response body
                ct = resp.headers.get('content-type', '')
                if 'json' in ct or 'text' in ct:
                    try:
                        body = resp.text()
                        # find any qnips or Mittagessen PDF URL in the body
                        urls = re.findall(
                            r'https://files\.qnips\.com/[^\s"\'\'<>]+\.pdf[^\s"\'\'<>]*',
                            body, re.I
                        )
                        if urls:
                            print(f"  [resp-body] PDF URLs: {urls[:3]}")
                            found_pdf.extend(urls)
                        else:
                            # Dump first 300 chars of body for debug
                            print(f"  [resp-body] (first 300): {body[:300]}")
                        found_api_json.append((u, body[:2000]))
                    except Exception as e:
                        print(f"  [resp-body] error reading body: {e}")

        page.on("request",  on_request)
        page.on("response", on_response)

        print(f"  Navigating to {url}")
        page.goto(url, wait_until="load", timeout=60000)

        # Wait longer for SPA + API calls to complete
        print("  Waiting 8s for API calls...")
        page.wait_for_timeout(8000)
        print(f"  Title: {page.title()}")

        html = page.content()
        browser.close()

    print(f"  HTML size: {len(html):,} bytes")
    print(f"  PDF URLs intercepted: {found_pdf}")

    # Also search raw HTML for any qnips URL
    html_pdfs = re.findall(
        r'https://files\.qnips\.com/[^\s"\'\'<>]+\.pdf[^\s"\'\'<>]*',
        html, re.I
    )
    if html_pdfs:
        print(f"  qnips PDFs in HTML: {html_pdfs[:5]}")
        found_pdf.extend(html_pdfs)

    # Debug: show visible body text
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script','style','noscript']): tag.decompose()
    body_txt = ' '.join(soup.get_text(' ').split())
    print(f"  Body text (500 chars): {body_txt[:500]}")

    pdf_url = found_pdf[0] if found_pdf else None
    return html, pdf_url

# ── Step 2: Download PDF ─────────────────────────────────────────────────────────
def download_pdf(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; KantinoBot/1.0)"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    print(f"  PDF downloaded: {len(r.content):,} bytes")
    return r.content

# ── Step 3: Extract text from PDF ─────────────────────────────────────────────
def extract_pdf_text(pdf_bytes: bytes) -> str:
    if pdfplumber:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                pages = [p.extract_text() or '' for p in pdf.pages]
            text = '\n'.join(pages)
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
    raise RuntimeError("No PDF parser available")

# ── Step 4: Parse qnips column layout ─────────────────────────────────────────
def dedup_text(text):
    for _ in range(6):
        text = re.sub(r'([A-Za-z\u00c0-\u017e,\- ]{4,})\1', r'\1', text)
    return text

def fix_name(name):
    name = re.sub(r'([a-z\u00e0-\u017e])([A-Z\u00c0-\u00de])', r'\1 \2', name)
    return re.sub(r'\s+', ' ', name).strip()

def detect_vv(name):
    low = name.lower()
    if 'vegan' in low: return 'VG'
    if 'vegetar' in low: return 'V'
    return ''

def split_items(body):
    parts = re.split(r'(?<=\d{2})  +(?=[A-Z\u00c0-\u00de])', body)
    result = []
    for part in parts:
        part = part.strip()
        if not part: continue
        m = re.search(r'(\d+,\d{2})(\d+,\d{2})\s*$', part)
        if not m:
            m = re.search(r'(\d+,\d{2})\s+(\d+,\d{2})\s*$', part)
        if m:
            name  = fix_name(part[:m.start()].strip())
            price = m.group(1) + ' \u20ac'
        else:
            name  = fix_name(part)
            price = ''
        if not name or len(name) < 3: continue
        low = name.lower()
        skip = ['oder','int.ext','mo - fr','alle preise','all prices',
                'restaurant','thomas','+49','allergen','monday','tuesday']
        if any(low.startswith(w) for w in skip): continue
        if re.match(r'^[\d.,\s\u20ac/]+$', name): continue
        result.append((name, price))
    return result

def parse_menu(pdf_text, local_dt):
    monday    = local_dt - timedelta(days=local_dt.weekday())
    day_labels = [f"{s} {(monday+timedelta(days=i)).strftime('%d.%m')}"
                  for i, s in enumerate(['Mo','Di','Mi','Do','Fr'])]

    text = ' '.join(pdf_text.split())
    text = dedup_text(text)
    text = re.sub(
        r'^.*?(?:Restaurant\s+Regensburg|\+49[^\s]+|\d{2}:\d{2}\s+Uhr)\s*',
        '', text, flags=re.I
    ).strip()

    items = split_items(text)
    print(f"  Items extracted: {len(items)}")
    for i, (n, p) in enumerate(items):
        print(f"    {i:2d}: {n[:60]!r:65} {p}")

    CATS = ['Suppe', 'Essen 1', 'Essen 2', 'Essen 3']
    week_data = {day: [] for day in day_labels}
    for ci, cat in enumerate(CATS):
        for di, day in enumerate(day_labels):
            idx = ci * 5 + di
            if idx < len(items):
                name, price = items[idx]
                week_data[day].append({
                    'kategorie': cat, 'name': name,
                    'vv': detect_vv(name), 'preis_int': price,
                })
    return {k: v for k, v in week_data.items() if v}

# ── Step 5: Render 800x600 landscape JPEG ───────────────────────────────────────
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

    # 1. Load page + intercept network
    print('Loading page...')
    html, pdf_url = load_page(url_menu)

    if not pdf_url:
        print('ERROR: No qnips PDF found -> placeholder.')
        img = render({}, kw, label, local, url_menu)
        img.save(str(out_path), 'JPEG', quality=92)
        return

    # 2. Download PDF
    print(f'Downloading PDF: {pdf_url[:100]}...')
    pdf_bytes = download_pdf(pdf_url)

    # 3. Extract text
    print('Extracting PDF text...')
    pdf_text = extract_pdf_text(pdf_bytes)

    # 4. Parse
    print('Parsing menu...')
    week_data = parse_menu(pdf_text, local)
    print(f'Days parsed: {list(week_data.keys())}')

    # 5. Render
    img = render(week_data, kw, label, local, url_menu)
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')


if __name__ == '__main__':
    main()
