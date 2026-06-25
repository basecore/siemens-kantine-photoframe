#!/usr/bin/env python3
"""Siemens Kantine Regensburg – 800x600 JPEG.

Actual page format (observed from flat text):
  - Category labels: 'Soup / Starter', 'Food 1', 'Food 2', 'Food 3'
    (English UI) or 'Suppe / Vorspeise', 'Essen 1' etc. (German UI)
  - Dish name spans multiple lines, then allergen codes (A, G, DAG …)
  - Price line: 'Int  €3.20' or 'Int  3,20 €' or 'Int\xa00,60\xa0€'
    i.e. "Int" and the price ARE on the same line (merged token).
  - Only today + future days visible; past days shown as grey.
"""
import os, re
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ── Config ───────────────────────────────────────────────────────────────────
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

# ── Colours ──────────────────────────────────────────────────────────────────
BLUE  = (0, 57, 107);    LIGHT = (0, 119, 193)
R_ODD = (240, 246, 252); R_EVEN = (255, 255, 255)
C_VG  = (34, 139, 34);  C_V   = (100, 180, 60)
C_TXT = (30, 30, 30);   WHITE  = (255, 255, 255)
GRID  = (200, 215, 230)
C_HOL_BG  = (220, 220, 220)
C_HOL_HDR = (140, 140, 140)
C_HOL_TXT = (100, 100, 100)
C_PAST_BG  = (235, 235, 235)
C_PAST_TXT = (160, 160, 160)

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

# ── Bavarian holidays ─────────────────────────────────────────────────────────
def _easter(y):
    a=y%19;b=y//100;c=y%100;d=b//4;e=b%4
    f=(b+8)//25;g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30
    i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7
    m=(a+11*h+22*l)//451
    mo=(h+l-7*m+114)//31; dy=((h+l-7*m+114)%31)+1
    return date(y,mo,dy)

def bavaria_holidays(y):
    e = _easter(y)
    return {
        date(y,1,1):    "Neujahr",
        date(y,1,6):    "Hl. Drei K\u00f6nige",
        e-timedelta(2): "Karfreitag",
        e:              "Ostersonntag",
        e+timedelta(1): "Ostermontag",
        date(y,5,1):    "Tag der Arbeit",
        e+timedelta(39):"Christi Himmelfahrt",
        e+timedelta(49):"Pfingstsonntag",
        e+timedelta(50):"Pfingstmontag",
        e+timedelta(60):"Fronleichnam",
        date(y,8,15):   "Mari\u00e4 Himmelfahrt",
        date(y,10,3):   "Tag der Deutschen Einheit",
        date(y,11,1):   "Allerheiligen",
        date(y,12,25):  "1. Weihnachtstag",
        date(y,12,26):  "2. Weihnachtstag",
    }

def week_holiday_map(local_dt):
    monday = local_dt.date() - timedelta(days=local_dt.weekday())
    y = monday.year
    hols = bavaria_holidays(y)
    if (monday+timedelta(4)).year != y:
        hols.update(bavaria_holidays(y+1))
    short = ['Mo','Di','Mi','Do','Fr']
    return {f"{short[i]} {(monday+timedelta(i)).strftime('%d.%m')}": hols.get(monday+timedelta(i))
            for i in range(5)}

# ── Time helpers ──────────────────────────────────────────────────────────────
def german_time(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Berlin"))
    except ImportError: pass
    import calendar; yr = dt.year
    def last_sun(y, m):
        ld = calendar.monthrange(y,m)[1]
        d2 = datetime(y,m,ld,tzinfo=timezone.utc)
        return d2 - timedelta(days=(d2.weekday()+1)%7)
    cs = last_sun(yr,3).replace(hour=1)
    ce = last_sun(yr,10).replace(hour=1)
    return dt + timedelta(hours=2 if cs<=dt<ce else 1)

def kw_label(dt):
    d = german_time(dt); y,w,_ = d.isocalendar()
    return f"{y}-W{w:02d}", int(w)

def week_dates(local_dt):
    monday = local_dt - timedelta(days=local_dt.weekday())
    return [(monday + timedelta(i)) for i in range(5)]

def day_key(dt_obj):
    short = ['Mo','Di','Mi','Do','Fr']
    return f"{short[dt_obj.weekday()]} {dt_obj.strftime('%d.%m')}"

# ── Parse flat innerText ──────────────────────────────────────────────────────
# Category headers (EN + DE, all slash variants)
CAT_HEADERS = {
    'Soup / Starter':    'Suppe',
    'Soup/Starter':      'Suppe',
    'Soup':              'Suppe',
    'Suppe / Vorspeise': 'Suppe',
    'Suppe/Vorspeise':   'Suppe',
    'Suppe':             'Suppe',
    'Food 1':   'Essen 1',
    'Food 2':   'Essen 2',
    'Food 3':   'Essen 3',
    'Essen 1':  'Essen 1',
    'Essen 2':  'Essen 2',
    'Essen 3':  'Essen 3',
    'Gericht 1':'Essen 1',
    'Gericht 2':'Essen 2',
    'Gericht 3':'Essen 3',
    'Fisch':    'Essen 3',
}

# Matches: 'Int\xa0 €3.20', 'Int  3,20 €', 'Int\xa00,60\xa0€', 'Int   €6.10'
INT_PRICE_RE = re.compile(
    r'Int[\s\u00a0]+[\u20ac$]?([0-9]+[.,][0-9]{2})'
    r'|Int[\s\u00a0]+([0-9]+[.,][0-9]{2})[\s\u00a0]*[\u20ac$]',
    re.IGNORECASE
)
ALLERGEN_RE = re.compile(r'^[A-Z]{1,10}$')
OR_RE       = re.compile(r'^(oder|or)$', re.IGNORECASE)
EXT_RE      = re.compile(r'^Ext[\s\u00a0]', re.IGNORECASE)

NOISE = {
    'Learn more','Got it!','home','Home','view_compact','Menu','place',
    'Stores','Impressum','close','Close','English','Lunch','filter_list',
    'Filter','Store','clear','Info','New Webportal','Register now:',
    'MyCasinoCard','Opening hours','edit','QR Code','Next','Previous',
    'Nutzungsbedingungen','Datenschutzerkl\u00e4rung',
    'Speiseplan','Mittagessen','Informationen','Deutsch',
    'Mehr erfahren','note','Aktuelle Woche',
    'view transactions','change card status','other features',
    'Go to MyCasinoCard','Use digital wallet to store QR Code',
    'QR Code for payment','edit your personal profile',
}

def parse_flat(lines: list) -> list:
    """Parse flat innerText lines → [{kategorie, name, preis_int, vv}]."""
    dishes    = []
    cur_cat   = None
    cur_name  = []
    seen_cats = set()

    def flush(price=''):
        nonlocal cur_name
        toks = [
            t for t in cur_name
            if not ALLERGEN_RE.match(t)
            and not OR_RE.match(t)
            and t not in NOISE
            and not INT_PRICE_RE.search(t)
            and not EXT_RE.match(t)
        ]
        # strip trailing allergen run
        while toks and ALLERGEN_RE.match(toks[-1]):
            toks.pop()
        name = ' '.join(toks).strip()
        if cur_cat and name and len(name) >= 3 and cur_cat not in seen_cats:
            low = name.lower()
            dishes.append({
                'kategorie': cur_cat,
                'name':      name,
                'preis_int': price + '\u00a0\u20ac' if price else '',
                'vv': 'VG' if 'vegan' in low else ('V' if 'vegetar' in low else '')
            })
            seen_cats.add(cur_cat)
        cur_name = []

    for line in lines:
        line = line.strip()
        if not line or line in NOISE:
            continue

        # Category header?
        if line in CAT_HEADERS:
            flush()
            cur_cat  = CAT_HEADERS[line]
            cur_name = []
            continue

        if cur_cat is None:
            continue

        # Int price line?
        m = INT_PRICE_RE.search(line)
        if m:
            raw = (m.group(1) or m.group(2) or '').replace(',','.')
            try:
                price_str = f"{float(raw):.2f}".replace('.',',')
            except ValueError:
                price_str = raw
            flush(price_str)
            continue

        # "oder"/"or" → we already committed (or will commit) first dish;
        # block second alternative by adding cat to seen_cats prematurely
        if OR_RE.match(line):
            seen_cats.add(cur_cat)
            cur_name = []
            continue

        # Ext price line → skip
        if EXT_RE.match(line):
            continue

        # Pure allergen token → skip
        if ALLERGEN_RE.match(line):
            continue

        cur_name.append(line)

    flush()
    return dishes


# ── JS: get innerText of the active mat-tab-body ──────────────────────────────
JS_TAB_TEXT = r"""
(function(){
  var panels = Array.from(document.querySelectorAll(
    'mat-tab-body, [role="tabpanel"], .mat-tab-body'));
  var active = panels.find(p => {
    var s = window.getComputedStyle(p);
    return s.display !== 'none' && s.visibility !== 'hidden' && p.offsetHeight > 0;
  }) || panels[0];
  if (!active) {
    var b = document.body.cloneNode(true);
    b.querySelectorAll('script,style,noscript').forEach(e=>e.remove());
    return b.innerText || b.textContent || '';
  }
  return active.innerText || active.textContent || '';
})()
"""

# ── Scrape one day ────────────────────────────────────────────────────────────
def scrape_day(page, date_obj) -> list:
    url = f"{URL_BASE}/date/{date_obj.strftime('%Y-%m-%d')}"
    if _SID: url += f"?ste_sid={_SID}"
    print(f"  Loading {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=60000)

    found = False
    for sel in [
        "text=Soup / Starter", "text=Food 1", "text=Food 2",
        "text=Suppe / Vorspeise", "text=Essen 1", "text=Suppe",
    ]:
        try:
            page.wait_for_selector(sel, timeout=10000)
            found = True
            print(f"    selector ok: {sel!r}")
            break
        except Exception:
            pass
    if not found:
        print("    no selector, extra wait 6s")
        page.wait_for_timeout(6000)
    else:
        page.wait_for_timeout(800)

    raw_text = page.evaluate(JS_TAB_TEXT) or ""
    lines    = [l.strip() for l in raw_text.splitlines() if l.strip()]
    print(f"    tab lines: {len(lines)}")

    if len(lines) < 5:
        full = page.evaluate(r"""
          (function(){
            var b=document.body.cloneNode(true);
            b.querySelectorAll('script,style,noscript').forEach(e=>e.remove());
            return b.innerText||b.textContent||'';
          })()
        """) or ""
        lines = [l.strip() for l in full.splitlines() if l.strip()]
        print(f"    fallback full-page lines: {len(lines)}")

    # Debug: first 25 lines
    for i,l in enumerate(lines[:25]):
        print(f"      {i:2d}: {l[:110]}")

    dishes = parse_flat(lines)
    print(f"    -> {[d['name'][:30] for d in dishes]}")
    return dishes


# ── Render ────────────────────────────────────────────────────────────────────
def wrap_text(draw, text, f, max_w):
    out2, cur2 = [], ''
    for w in text.split():
        t = (cur2+' '+w).strip() if cur2 else w
        b = draw.textbbox((0,0), t, font=f)
        if b[2]-b[0] <= max_w: cur2 = t
        else:
            if cur2: out2.append(cur2)
            cur2 = w
    if cur2: out2.append(cur2)
    return out2

CATS = ['Suppe','Essen 1','Essen 2','Essen 3']

def render(week_data, kw, label, local_dt, url_menu, holiday_map,
           today_date, source=''):
    img = Image.new('RGB', (W,H), (255,255,255))
    d   = ImageDraw.Draw(img)
    ftit=lf(14,True); fday=lf(11,True); fcat=lf(8,True)
    ftxt=lf(9);       fbdg=lf(8,True);  fprc=lf(8); fftr=lf(9)
    HDR_H=36; LEGEND_H=17

    d.rectangle([(0,0),(W,HDR_H)], fill=BLUE)
    title = f'Siemens Kantine Regensburg  |  KW {kw:02d}'
    b = d.textbbox((0,0), title, font=ftit)
    d.text(((W-(b[2]-b[0]))//2,(HDR_H-(b[3]-b[1]))//2), title, font=ftit, fill=WHITE)
    y = HDR_H

    all_days = list(holiday_map.keys())
    dw = W//len(all_days); DAY_H=20
    for i,day in enumerate(all_days):
        x = i*dw
        is_hol  = holiday_map[day] is not None
        is_past = _is_past(day, today_date)
        hdr_col = C_HOL_HDR if is_hol else (C_PAST_TXT if is_past else LIGHT)
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)], fill=hdr_col)
        b = d.textbbox((0,0), day, font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2,y+(DAY_H-(b[3]-b[1]))//2),
               day, font=fday, fill=WHITE)
        if i>0: d.line([(x,y),(x,y+DAY_H)], fill=BLUE, width=1)
    y += DAY_H

    avail = H-y-FOOTER_H-LEGEND_H-2
    rs    = int(avail*0.20)
    re_   = (avail-rs)//3
    ROW_H = {'Suppe':rs,'Essen 1':re_,'Essen 2':re_,'Essen 3':avail-rs-2*re_}

    for ri,cat in enumerate(CATS):
        rh = ROW_H[cat]
        d.line([(0,y),(W,y)], fill=GRID, width=1)
        for i,day in enumerate(all_days):
            x = i*dw
            is_hol  = holiday_map[day] is not None
            is_past = _is_past(day, today_date)
            bg = C_PAST_BG if is_past else (C_HOL_BG if is_hol else (R_ODD if ri%2==0 else R_EVEN))
            d.rectangle([(x,y),(x+dw-1,y+rh-1)], fill=bg)
            if i>0: d.line([(x,y),(x,y+rh)], fill=GRID, width=1)

            if is_past:
                if ri==0:
                    b=d.textbbox((0,0),'vergangen',font=fprc)
                    d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-6),
                           'vergangen',font=fprc,fill=C_PAST_TXT)
                continue

            if is_hol:
                if ri==0:
                    hname=holiday_map[day]
                    b=d.textbbox((0,0),'Feiertag',font=fbdg)
                    bw=b[2]-b[0]+6; bh=b[3]-b[1]+4
                    bx=x+(dw-bw)//2; by=y+6
                    d.rounded_rectangle([(bx,by),(bx+bw,by+bh)],radius=3,fill=(160,160,160))
                    d.text((bx+3,by+2),'Feiertag',font=fbdg,fill=WHITE)
                    cy=by+bh+4
                    for ln in wrap_text(d,hname,ftxt,dw-6)[:3]:
                        b2=d.textbbox((0,0),ln,font=ftxt)
                        d.text((x+(dw-(b2[2]-b2[0]))//2,cy),ln,font=ftxt,fill=C_HOL_TXT)
                        cy+=11
                continue

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
                bw=b[2]-b[0]+5; bh2=b[3]-b[1]+3
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh2)],radius=3,fill=bc)
                d.text((cx+3,cy+1),bl,font=fbdg,fill=WHITE); cy+=bh2+2
            for ln in wrap_text(d,it['name'],ftxt,avw)[:3]:
                d.text((cx,cy),ln,font=ftxt,fill=C_TXT); cy+=11
            if it['preis_int']:
                pl=f"Int: {it['preis_int']}"
                b=d.textbbox((0,0),pl,font=fprc)
                d.text((x+dw-(b[2]-b[0])-3,y+rh-(b[3]-b[1])-3),pl,font=fprc,fill=LIGHT)
        y+=rh

    d.line([(0,y),(W,y)],fill=GRID,width=1); y+=1
    d.rectangle([(0,y),(W,y+LEGEND_H)],fill=(245,249,253))
    d.rectangle([(5,y+4),(15,y+13)],fill=C_VG)
    d.text((19,y+3),'Vegan',font=fprc,fill=C_TXT)
    d.rectangle([(65,y+4),(75,y+13)],fill=C_V)
    d.text((79,y+3),'Vegetarisch',font=fprc,fill=C_TXT)
    d.rectangle([(150,y+4),(160,y+13)],fill=C_HOL_HDR)
    d.text((164,y+3),'Feiertag',font=fprc,fill=C_TXT)
    d.rectangle([(225,y+4),(235,y+13)],fill=C_PAST_BG)
    d.text((239,y+3),'vergangen',font=fprc,fill=C_TXT)
    d.text((315,y+3),'Int = Mitarbeiterpreis',font=fprc,fill=(120,120,120))
    _footer(d,kw,label,local_dt,fftr,source)
    return img

def _is_past(day_key_str, today_date):
    try:
        dm = day_key_str.split(' ')[1].split('.')
        return date(today_date.year, int(dm[1]), int(dm[0])) < today_date
    except Exception:
        return False

def _footer(d,kw,label,local_dt,f,source=''):
    src=f' \u2013 {source}' if source else ''
    txt=(f'KW {kw:02d} / {label}  \u2013  '
         f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  \u2013  "
         f"siemens.cateringportal.io{src}")
    d.rectangle([(0,H-FOOTER_H),(W,H)],fill=BLUE)
    b=d.textbbox((0,0),txt,font=f)
    d.text(((W-(b[2]-b[0]))//2,H-16),txt,font=f,fill=WHITE)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now        = datetime.now(timezone.utc)
    local      = german_time(now)
    label, kw  = kw_label(now)
    today_date = local.date()
    out_path   = OUT_DIR / f'kantine_{label}.jpg'

    print(f'Week label : {label}  (KW {kw:02d})')
    print(f'Today      : {today_date}')

    holiday_map  = week_holiday_map(local)
    hol_days     = [k for k,v in holiday_map.items() if v]
    scrape_dates = [
        d for d in week_dates(local)
        if d.date() >= today_date and day_key(d) not in hol_days
    ]

    print(f'Feiertage  : {[(k,holiday_map[k]) for k in hol_days] or "keine"}')
    print(f'Scraping   : {[day_key(d) for d in scrape_dates]}')

    week_data = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width":1400,"height":900},
            extra_http_headers={"Accept-Language":"de-DE,de;q=0.9"},
        )
        for date_obj in scrape_dates:
            dk     = day_key(date_obj)
            dishes = scrape_day(page, date_obj)
            if dishes:
                week_data[dk] = dishes
        browser.close()

    days_filled = len(week_data)
    days_avail  = len(scrape_dates)
    print(f'Ergebnis   : {list(week_data.keys())}  ({days_filled}/{days_avail})')

    img = render(week_data, kw, label, local,
                 URL_BASE, holiday_map, today_date,
                 f'DOM ({days_filled}/{days_avail} Tage)')
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')

if __name__ == '__main__':
    main()
