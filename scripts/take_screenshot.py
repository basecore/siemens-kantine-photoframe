#!/usr/bin/env python3
"""Siemens Kantine Regensburg – weekly menu PDF scraper + 800x600 JPEG renderer."""
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
URL_BASE = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID = os.environ.get("CATERINGPORTAL_SID", "").strip()

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8
W, H = 800, 600   # landscape
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

# ── Step 1: Load HTML with Playwright (SPA-aware) ────────────────────────────
def load_html(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1280, "height": 900},
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        # Intercept and log PDF links as they are requested
        pdf_urls_seen = []
        def on_request(req):
            if '.pdf' in req.url.lower():
                print(f"  [network] PDF request: {req.url[:120]}")
                pdf_urls_seen.append(req.url)
        page.on("request", on_request)

        page.goto(url, wait_until="load", timeout=60000)

        # Wait for SPA to fully render: look for qnips link OR dish-like text
        print("  Waiting for SPA content...")
        for selector in [
            "a[href*='qnips']",
            "a[href*='.pdf']",
            "[class*='menu']",
            "[class*='dish']",
            "[class*='meal']",
            "main",
        ]:
            try:
                page.wait_for_selector(selector, timeout=8000, state="attached")
                print(f"  Selector matched: {selector}")
                break
            except Exception:
                pass

        # Extra wait for JS-rendered content
        page.wait_for_timeout(5000)
        print(f"  Final title: {page.title()}")

        html = page.content()
        browser.close()

    print(f"  HTML size: {len(html):,} bytes")
    print(f"  PDF URLs seen in network: {pdf_urls_seen}")

    # Debug: show all href containing 'pdf' or 'qnips'
    pdf_hrefs = re.findall(r'href=["\']([^"\'>]*(?:pdf|qnips)[^"\'>]*)["\']', html, re.I)
    print(f"  PDF/qnips hrefs in HTML ({len(pdf_hrefs)}):")
    for h in pdf_hrefs[:10]:
        print(f"    {h[:120]}")

    # Also show first 2000 chars of body text for debugging
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script','style','noscript']):
        tag.decompose()
    body_text = ' '.join(soup.get_text(' ').split())
    print(f"  Body text preview (first 500 chars): {body_text[:500]}")

    return html, pdf_urls_seen

# ── Step 2: Find qnips PDF URL ────────────────────────────────────────────────────
def find_pdf_url(html: str, network_urls: list, kw: int) -> str | None:
    # 1. Check network-intercepted PDF URLs first (most reliable)
    for url in network_urls:
        if 'qnips' in url.lower() and 'mittagessen' in url.lower():
            print(f"  PDF from network intercept: {url[:120]}")
            return url

    # 2. Search HTML for qnips Mittagessen PDF links
    patterns = [
        r'(https://files\.qnips\.com/release-menu-pdfs/Mittagessen[^"\s\'<>]+)',
        r'(https://files\.qnips\.com/[^"\s\'<>]+Mittagessen[^"\s\'<>]+\.pdf[^"\s\'<>]*)',
        r'(https?://[^"\s\'<>]*qnips[^"\s\'<>]*Mittagessen[^"\s\'<>]*\.pdf[^"\s\'<>]*)',
        r'(https?://[^"\s\'<>]*qnips[^"\s\'<>]*\.pdf[^"\s\'<>]*)',
    ]
    for pat in patterns:
        matches = re.findall(pat, html, re.I)
        for url in matches:
            if f'_{kw}_' in url or f'_DE_{kw}_' in url:
                print(f"  PDF found (KW {kw} match): {url[:120]}")
                return url
        if matches:
            print(f"  PDF found (first match): {matches[0][:120]}")
            return matches[0]

    print("  No qnips PDF URL found.")
    return None

# ── Step 3: Download PDF ─────────────────────────────────────────────────────────
def download_pdf(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; KantinoBot/1.0)"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    print(f"  PDF downloaded: {len(r.content):,} bytes")
    return r.content

# ── Step 4: Extract text from PDF ──────────────────────────────────────────────
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

# ── Step 5: Parse qnips column layout ─────────────────────────────────────────
def dedup_text(text: str) -> str:
    for _ in range(6):
        text = re.sub(r'([A-Za-z\u00c0-\u017e,\- ]{4,})\1', r'\1', text)
    return text

def fix_name(name: str) -> str:
    name = re.sub(r'([a-z\u00e0-\u017e])([A-Z\u00c0-\u00de])', r'\1 \2', name)
    return re.sub(r'\s+', ' ', name).strip()

def detect_vv(name: str) -> str:
    low = name.lower()
    if 'vegan' in low: return 'VG'
    if 'vegetar' in low: return 'V'
    return ''

def split_items(body: str) -> list:
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
        skip = ['oder','int','ext','int.ext','mo - fr','alle preise','all prices',
                'restaurant','thomas','+49','allergen','monday','tuesday']
        if any(low.startswith(w) for w in skip): continue
        if re.match(r'^[\d.,\s\u20ac/]+$', name): continue
        result.append((name, price))
    return result

def parse_menu(pdf_text: str, local_dt: datetime) -> dict:
    monday   = local_dt - timedelta(days=local_dt.weekday())
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

# ── Step 6: Render 800x600 landscape JPEG ───────────────────────────────────────
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
    ftit = lf(14,True); fday = lf(11,True); fcat = lf(8,True)
    ftxt = lf(9); fbdg = lf(8,True); fprc = lf(8); fftr = lf(9)

    HDR_H=36; LEGEND_H=17
    d.rectangle([(0,0),(W,HDR_H)],fill=BLUE)
    title = f'Siemens Kantine Regensburg  |  KW {kw:02d}'
    b = d.textbbox((0,0),title,font=ftit)
    d.text(((W-(b[2]-b[0]))//2,(HDR_H-(b[3]-b[1]))//2),title,font=ftit,fill=WHITE)
    y = HDR_H

    if not week_data:
        d.text((20,y+40),'Speiseplan konnte nicht geladen werden.',font=ftxt,fill=C_TXT)
        d.text((20,y+60),'Bitte manuell pr\u00fcfen:',font=ftxt,fill=C_TXT)
        d.text((20,y+80),url_menu,font=ftxt,fill=LIGHT)
        _footer(d,kw,label,local_dt,fftr); return img

    days = list(week_data.keys())[:5]
    dw   = W // len(days)
    DAY_H = 20
    for i,day in enumerate(days):
        x=i*dw
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)],fill=LIGHT)
        b=d.textbbox((0,0),day,font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2,y+(DAY_H-(b[3]-b[1]))//2),day,font=fday,fill=WHITE)
        if i>0: d.line([(x,y),(x,y+DAY_H)],fill=BLUE,width=1)
    y+=DAY_H

    avail = H-y-FOOTER_H-LEGEND_H-2
    rs = int(avail*0.20)
    re_ = (avail-rs)//3
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

    # Use date-specific URL so the SPA loads current week's content
    date_str  = local.strftime('%Y-%m-%d')
    url_menu  = f"{URL_BASE}/date/{date_str}"
    if _SID:
        url_menu += f"?ste_sid={_SID}"

    print(f'Target URL : {url_menu}')
    print(f'Week label : {label}  (KW {kw:02d})')

    # 1. Load HTML
    print('Loading page...')
    html, network_pdfs = load_html(url_menu)

    # 2. Find PDF URL
    pdf_url = find_pdf_url(html, network_pdfs, kw)
    if not pdf_url:
        print('ERROR: No qnips PDF found -> placeholder.')
        img = render({}, kw, label, local, url_menu)
        img.save(str(out_path), 'JPEG', quality=92)
        return

    # 3. Download PDF
    print('Downloading PDF...')
    pdf_bytes = download_pdf(pdf_url)

    # 4. Extract text
    print('Extracting PDF text...')
    pdf_text = extract_pdf_text(pdf_bytes)

    # 5. Parse menu
    print('Parsing menu...')
    week_data = parse_menu(pdf_text, local)
    print(f'Days parsed: {list(week_data.keys())}')

    # 6. Render
    img = render(week_data, kw, label, local, url_menu)
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')


if __name__ == '__main__':
    main()
