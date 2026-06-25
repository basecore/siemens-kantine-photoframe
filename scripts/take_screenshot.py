#!/usr/bin/env python3
"""Siemens Kantine Regensburg – 800x600 JPEG from cateringportal.io DOM.

Scraping strategy (no PDF needed):
  1. Playwright loads the Monday-URL so all 5 days are in the DOM.
  2. Injected JS walks the rendered Angular SPA and extracts:
       - Tab labels (day names + dates)
       - Per-tab: category rows (Suppe / Essen 1-3) and their dishes
       - Per-dish: name, int-price, vegan/vegetarian flag
  3. Multiple JS extraction strategies tried in order:
       A. mat-tab / Angular Material tabs  (most likely)
       B. role=tabpanel panels
       C. Flat text with Int/Ext price as day separator
  4. Bavarian public holidays are computed and shown as grey columns.
"""
import json, os, re
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ── Config ───────────────────────────────────────────────────────────────────
URL_BASE = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID     = os.environ.get("CATERINGPORTAL_SID", "").strip()

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8
W, H     = 800, 600
FOOTER_H = 22

# ── Colours ──────────────────────────────────────────────────────────────────
BLUE  = (0, 57, 107);   LIGHT = (0, 119, 193)
R_ODD = (240, 246, 252); R_EVEN = (255, 255, 255)
C_VG  = (34, 139, 34);  C_V    = (100, 180, 60)
C_TXT = (30, 30, 30);   WHITE  = (255, 255, 255)
GRID  = (200, 215, 230)
C_HOL_BG  = (220, 220, 220)
C_HOL_HDR = (140, 140, 140)
C_HOL_TXT = (100, 100, 100)

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
    a=y%19; b=y//100; c=y%100; d=b//4; e=b%4
    f=(b+8)//25; g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30
    i=c//4; k=c%4; l=(32+2*e+2*i-h-k)%7
    m=(a+11*h+22*l)//451
    mo=(h+l-7*m+114)//31; dy=((h+l-7*m+114)%31)+1
    return date(y, mo, dy)

def bavaria_holidays(y):
    e = _easter(y)
    return {
        date(y,1,1):   "Neujahr",
        date(y,1,6):   "Hl. Drei K\u00f6nige",
        e-timedelta(2):"Karfreitag",
        e:             "Ostersonntag",
        e+timedelta(1):"Ostermontag",
        date(y,5,1):   "Tag der Arbeit",
        e+timedelta(39):"Christi Himmelfahrt",
        e+timedelta(49):"Pfingstsonntag",
        e+timedelta(50):"Pfingstmontag",
        e+timedelta(60):"Fronleichnam",
        date(y,8,15):  "Mari\u00e4 Himmelfahrt",
        date(y,10,3):  "Tag der Deutschen Einheit",
        date(y,11,1):  "Allerheiligen",
        date(y,12,25): "1. Weihnachtstag",
        date(y,12,26): "2. Weihnachtstag",
    }

def week_holiday_map(local_dt):
    monday = local_dt.date() - timedelta(days=local_dt.weekday())
    y      = monday.year
    hols   = bavaria_holidays(y)
    if (monday+timedelta(4)).year != y:
        hols.update(bavaria_holidays(y+1))
    short  = ['Mo','Di','Mi','Do','Fr']
    return {f"{short[i]} {(monday+timedelta(i)).strftime('%d.%m')}": hols.get(monday+timedelta(i))
            for i in range(5)}

# ── Time ──────────────────────────────────────────────────────────────────────
def german_time(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Berlin"))
    except ImportError: pass
    import calendar
    yr = dt.year
    def last_sun(y,m):
        ld=calendar.monthrange(y,m)[1]
        d2=datetime(y,m,ld,tzinfo=timezone.utc)
        return d2-timedelta(days=(d2.weekday()+1)%7)
    cs=last_sun(yr,3).replace(hour=1); ce=last_sun(yr,10).replace(hour=1)
    return dt+timedelta(hours=2 if cs<=dt<ce else 1)

def kw_label(dt):
    d=german_time(dt); y,w,_=d.isocalendar()
    return f"{y}-W{w:02d}", int(w)

def day_keys(local_dt):
    monday=local_dt-timedelta(days=local_dt.weekday())
    short=['Mo','Di','Mi','Do','Fr']
    return [f"{short[i]} {(monday+timedelta(i)).strftime('%d.%m')}" for i in range(5)]

# ── JS extraction scripts ──────────────────────────────────────────────────────

# Strategy A: Angular Material mat-tab-group
JS_MAT_TABS = """
(function(){
  var result = {};
  // Find all mat-tab-label elements for day names
  var labels = Array.from(document.querySelectorAll(
    'mat-tab-header .mat-tab-label, mat-tab-header .mdc-tab, [role="tab"]'
  ));
  var bodies  = Array.from(document.querySelectorAll(
    'mat-tab-body, .mat-tab-body, [role="tabpanel"]'
  ));
  if (!labels.length || !bodies.length) return null;

  for (var ti = 0; ti < Math.min(labels.length, bodies.length); ti++) {
    var dayLabel = labels[ti].textContent.trim().replace(/\\s+/g,' ');
    if (!dayLabel) continue;
    var body  = bodies[ti];
    var dishes = [];

    // Each category row: look for headings like 'Food 1', 'Soup / Starter' etc.
    var catNodes = body.querySelectorAll(
      '[class*="category"], [class*="course"], [class*="menu-item"], ' +
      '[class*="meal"], [class*="dish"], tr, li'
    );

    // Fallback: just grab all text nodes with prices
    var allText = body.innerText || body.textContent || '';
    var lines   = allText.split('\\n').map(s=>s.trim()).filter(Boolean);

    var catMap = {
      'Soup / Starter':'Suppe','Soup/Starter':'Suppe','Soup':'Suppe',
      'Food 1':'Essen 1','Food 2':'Essen 2','Food 3':'Essen 3',
      'Essen 1':'Essen 1','Essen 2':'Essen 2','Essen 3':'Essen 3'
    };
    var curCat = null, curToks = [], priceRe = /^\\d+[.,]\\d{2}$/, intRe = /^Int$/i, extRe = /^Ext$/i;
    for (var li2=0; li2<lines.length; li2++){
      var tok = lines[li2];
      if (catMap[tok]) { curCat=catMap[tok]; curToks=[]; continue; }
      if (!curCat) continue;
      if (intRe.test(tok)) {
        var nextTok = lines[li2+1] || '';
        if (priceRe.test(nextTok.replace(/[\u20ac$]/,''))) {
          var price = nextTok.replace(/[\u20ac$]/,'');
          var name  = curToks.filter(t=>!/^[A-H](\\.[1-6])?$/.test(t)&&!/^(or|oder)$/i.test(t)).join(' ').trim();
          if (name.length>=3) {
            var low=name.toLowerCase();
            dishes.push({cat:curCat, name:name, price:price,
              vv: low.includes('vegan')?'VG': low.includes('vegetar')?'V':''});
          }
          curToks=[]; li2+=1;
          if ((lines[li2+1]||'').match(/^Ext$/i)) li2+=2;
          continue;
        }
      }
      if (!extRe.test(tok) && !priceRe.test(tok.replace(/[\u20ac$]/,''))) curToks.push(tok);
    }
    result[dayLabel] = dishes;
  }
  return Object.keys(result).length ? result : null;
})()
"""

# Strategy B: find day containers by looking for date pattern DD.MM inside any block
JS_DATE_BLOCKS = """
(function(){
  var result = {};
  var dateRe  = /\\b(\\d{2}\\.\\d{2})\\.?\\b/;
  var priceRe = /^\\d+[.,]\\d{2}$/;
  var intRe   = /^Int$/i, extRe=/^Ext$/i;
  var catMap  = {
    'Soup / Starter':'Suppe','Soup/Starter':'Suppe','Soup':'Suppe',
    'Food 1':'Essen 1','Food 2':'Essen 2','Food 3':'Essen 3',
    'Essen 1':'Essen 1','Essen 2':'Essen 2','Essen 3':'Essen 3'
  };

  // Walk all block-level elements, find those that contain a date in their heading
  var candidates = document.querySelectorAll(
    'section, article, div[class*="day"], div[class*="column"], '
    + 'div[class*="tab"], td[class*="day"], div[class*="week"] > div'
  );

  candidates.forEach(function(el){
    var hd = el.querySelector('h1,h2,h3,h4,h5,th,strong,[class*="header"],[class*="title"]');
    if (!hd) return;
    var hdTxt = hd.textContent.trim();
    var dm = hdTxt.match(dateRe); if (!dm) return;

    var lines = (el.innerText||el.textContent||'').split('\\n').map(s=>s.trim()).filter(Boolean);
    var dishes=[], curCat=null, curToks=[];
    for (var i=0;i<lines.length;i++) {
      var tok=lines[i];
      if (catMap[tok]){curCat=catMap[tok];curToks=[];continue;}
      if (!curCat) continue;
      if (intRe.test(tok) && priceRe.test((lines[i+1]||'').replace(/[\u20ac$]/,''))){
        var price=(lines[i+1]||'').replace(/[\u20ac$]/,'');
        var name=curToks.filter(t=>!/^[A-H](\\.[1-6])?$/.test(t)&&!/^(or|oder)$/i.test(t)).join(' ').trim();
        if(name.length>=3){
          var low=name.toLowerCase();
          dishes.push({cat:curCat,name:name,price:price,
            vv:low.includes('vegan')?'VG':low.includes('vegetar')?'V':''});
        }
        curToks=[];i+=1;
        if((lines[i+1]||'').match(/^Ext$/i))i+=2;
        continue;
      }
      if(!extRe.test(tok)&&!priceRe.test(tok.replace(/[\u20ac$]/,''))) curToks.push(tok);
    }
    if(dishes.length) result[hdTxt]=dishes;
  });
  return Object.keys(result).length>=3 ? result : null;
})()
"""

# Strategy C: full-page flat text, use day-header lines as separators
JS_FLAT = """
(function(){
  // Remove script/style nodes
  var clone = document.body.cloneNode(true);
  clone.querySelectorAll('script,style,noscript').forEach(e=>e.remove());
  var text = clone.innerText || clone.textContent || '';
  return text;
})()
"""

# ── Playwright scraper ─────────────────────────────────────────────────────────
def scrape(url: str, keys: list[str]) -> tuple[dict, str]:
    """
    Returns (week_data, strategy_name).
    week_data = {day_key: [{kategorie, name, preis_int, vv}, ...]}
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1400, "height": 900},
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9"},
        )
        page.goto(url, wait_until="networkidle", timeout=60000)
        print("  Waiting for menu content...")
        # Wait for either German or English category label
        for sel in ["text=Food 1","text=Essen 1","text=Food 2","text=Suppe"]:
            try: page.wait_for_selector(sel, timeout=10000); print(f"  found: {sel!r}"); break
            except Exception: pass
        # Extra wait for lazy Angular rendering
        page.wait_for_timeout(3000)
        print(f"  Title: {page.title()}")

        # --- Strategy A: mat-tabs JS ---
        raw = page.evaluate(JS_MAT_TABS)
        if raw and isinstance(raw, dict) and len(raw) >= 3:
            browser.close()
            return _map_js_result(raw, keys), "DOM-MatTabs"
        print("  StratA failed, trying StratB...")

        # --- Strategy B: date-block JS ---
        raw = page.evaluate(JS_DATE_BLOCKS)
        if raw and isinstance(raw, dict) and len(raw) >= 3:
            browser.close()
            return _map_js_result(raw, keys), "DOM-DateBlocks"
        print("  StratB failed, trying StratC (flat)...")

        # --- Strategy C: flat text ---
        flat_text = page.evaluate(JS_FLAT) or ""

        # Also dump HTML for debugging
        html = page.content()
        browser.close()

    print(f"  Flat text length: {len(flat_text)}")
    week_data = _parse_flat(flat_text, keys)
    if week_data:
        return week_data, "DOM-Flat"

    # Last resort: print first 120 lines for debugging
    print("  All strategies failed. First 120 lines of flat text:")
    for i, l in enumerate(flat_text.splitlines()[:120]):
        print(f"    {i:3d}: {l[:120]}")
    return {}, "FAILED"


def _map_js_result(raw: dict, keys: list[str]) -> dict:
    """
    Maps JS result {label: [{cat,name,price,vv}]} to week_data {day_key: [dish_dicts]}.
    Tries to match labels to day_keys by DD.MM date substring.
    """
    date_re = re.compile(r'(\d{2}\.\d{2})')
    # Build lookup: '22.06' -> 'Mo 22.06'
    key_by_date = {}
    for k in keys:
        m = date_re.search(k)
        if m: key_by_date[m.group(1)] = k

    week_data = {}
    for label, dishes in raw.items():
        # Match by date in label
        m = date_re.search(label)
        day_key = key_by_date.get(m.group(1)) if m else None
        if not day_key:
            # Try positional match (0->Mo, 1->Di ...)
            idx = list(raw.keys()).index(label)
            day_key = keys[idx] if idx < len(keys) else None
        if not day_key: continue

        CAT_ORDER = ['Suppe','Essen 1','Essen 2','Essen 3']
        # Group dishes by category, keep first per cat
        by_cat = {}
        for d in dishes:
            cat = d.get('cat','Essen 1')
            if cat not in by_cat: by_cat[cat] = d
        week_data[day_key] = [
            {'kategorie': cat, 'name': by_cat[cat]['name'],
             'preis_int': by_cat[cat].get('price','') + ' \u20ac' if by_cat[cat].get('price') else '',
             'vv': by_cat[cat].get('vv','')}
            for cat in CAT_ORDER if cat in by_cat
        ]
        print(f"  {day_key}: {[d['name'][:30] for d in week_data[day_key]]}")
    return week_data


def _parse_flat(text: str, open_days: list[str]) -> dict:
    """
    Flat-text fallback.
    Category headers separate blocks; Int+price = one day boundary.
    """
    CAT_HEADERS = {
        'Soup / Starter':'Suppe','Soup/Starter':'Suppe','Soup':'Suppe',
        'Food 1':'Essen 1','Food 2':'Essen 2','Food 3':'Essen 3',
        'Essen 1':'Essen 1','Essen 2':'Essen 2','Essen 3':'Essen 3',
        'Gericht 1':'Essen 1','Gericht 2':'Essen 2','Gericht 3':'Essen 3',
    }
    UI_NOISE = {
        'Learn more','Got it!','home','Home','view_compact','Menu','place',
        'Stores','Impressum','close','Close','English','menu','Lunch',
        'filter_list','Filter','Store','clear','Info','New Webportal',
        'Register now:','MyCasinoCard','Opening hours','edit your personal profile',
        'view transactions','QR Code for payment','Use digital wallet to store QR Code',
        'change card status','other features','Go to MyCasinoCard',
    }
    INT_RE  = re.compile(r'^Int$', re.I)
    EXT_RE  = re.compile(r'^Ext$', re.I)
    PRIC_RE = re.compile(r'^[\u20ac$]?\d+[.,]\d{2}$')
    ALLG_RE = re.compile(r'^[A-H](\.[1-6])?$')
    OR_RE   = re.compile(r'^(or|oder)$', re.I)

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    menu_start = next((i for i,l in enumerate(lines) if l in CAT_HEADERS), None)
    if menu_start is None: return {}
    print(f"  [flat] menu starts at line {menu_start}: {lines[menu_start]!r}")

    blocks, cur_cat, cur_lines = [], None, []
    for line in lines[menu_start:]:
        if line in CAT_HEADERS:
            if cur_cat: blocks.append((cur_cat, cur_lines))
            cur_cat, cur_lines = CAT_HEADERS[line], []
        elif cur_cat: cur_lines.append(line)
    if cur_cat: blocks.append((cur_cat, cur_lines))
    print(f"  [flat] blocks: {[b[0] for b in blocks]}")

    n = len(open_days)
    week_data = {k: [] for k in open_days}
    for cat_label, raw in blocks:
        cur, day_idx, i = [], 0, 0
        while i < len(raw) and day_idx < n:
            tok = raw[i]
            if INT_RE.match(tok) and i+1 < len(raw) and PRIC_RE.match(raw[i+1]):
                price = raw[i+1].lstrip('\u20ac$') + ' \u20ac'
                name_toks = [t for t in cur if not ALLG_RE.match(t) and not OR_RE.match(t)]
                name = ' '.join(name_toks).strip()
                if name and len(name) >= 3:
                    low = name.lower()
                    week_data[open_days[day_idx]].append({
                        'kategorie': cat_label, 'name': name, 'preis_int': price,
                        'vv': 'VG' if 'vegan' in low else ('V' if 'vegetar' in low else '')
                    })
                cur = []; day_idx += 1; i += 2
                if i < len(raw) and EXT_RE.match(raw[i]):
                    i += 1
                    if i < len(raw) and PRIC_RE.match(raw[i]): i += 1
                continue
            if EXT_RE.match(tok):
                i += 1
                if i < len(raw) and PRIC_RE.match(raw[i]): i += 1
                continue
            if tok in CAT_HEADERS or tok in UI_NOISE: break
            if not ALLG_RE.match(tok): cur.append(tok)
            i += 1
        print(f"  [flat] {cat_label}: {day_idx}/{n} days")
    return {k: v for k, v in week_data.items() if v}


# ── Render ──────────────────────────────────────────────────────────────────────
def wrap_text(draw, text, f, max_w):
    words=text.split(); out,cur=[],[]
    for w in words:
        t=(cur+' '+w if cur else w).strip() if isinstance(cur,str) else ' '.join(cur+[w])
        # simpler version
        t=((' '.join(out[-1:]+[w])) if out else w)  # unused, rebuild below
    # clean rebuild
    out2, cur2 = [], ''
    for w in words:
        t=(cur2+' '+w).strip() if cur2 else w
        b=draw.textbbox((0,0),t,font=f)
        if b[2]-b[0]<=max_w: cur2=t
        else:
            if cur2: out2.append(cur2)
            cur2=w
    if cur2: out2.append(cur2)
    return out2

CATS = ['Suppe','Essen 1','Essen 2','Essen 3']

def render(week_data, kw, label, local_dt, url_menu, holiday_map, source=''):
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

    if not week_data and not any(v for v in holiday_map.values() if v):
        d.text((20,y+40),'Speiseplan nicht verf\u00fcgbar.',font=ftxt,fill=C_TXT)
        d.text((20,y+60),url_menu,font=ftxt,fill=LIGHT)
        _footer(d,kw,label,local_dt,fftr,source); return img

    all_days=list(holiday_map.keys())  # Mo..Fr always 5
    dw=W//len(all_days); DAY_H=20
    for i,day in enumerate(all_days):
        x=i*dw; is_hol=holiday_map[day] is not None
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)],fill=C_HOL_HDR if is_hol else LIGHT)
        b=d.textbbox((0,0),day,font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2,y+(DAY_H-(b[3]-b[1]))//2),day,font=fday,fill=WHITE)
        if i>0: d.line([(x,y),(x,y+DAY_H)],fill=BLUE,width=1)
    y+=DAY_H

    avail=H-y-FOOTER_H-LEGEND_H-2
    rs=int(avail*0.20); re_=(avail-rs)//3
    ROW_H={'Suppe':rs,'Essen 1':re_,'Essen 2':re_,'Essen 3':avail-rs-2*re_}

    for ri,cat in enumerate(CATS):
        rh=ROW_H[cat]
        d.line([(0,y),(W,y)],fill=GRID,width=1)
        for i,day in enumerate(all_days):
            x=i*dw; is_hol=holiday_map[day] is not None
            bg=C_HOL_BG if is_hol else (R_ODD if ri%2==0 else R_EVEN)
            d.rectangle([(x,y),(x+dw-1,y+rh-1)],fill=bg)
            if i>0: d.line([(x,y),(x,y+rh)],fill=GRID,width=1)

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
    d.text((220,y+3),'Int = Mitarbeiterpreis',font=fprc,fill=(120,120,120))
    _footer(d,kw,label,local_dt,fftr,source)
    return img

def _footer(d,kw,label,local_dt,f,source=''):
    src=f' \u2013 {source}' if source else ''
    txt=(f'KW {kw:02d} / {label}  \u2013  '
         f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  \u2013  "
         f"siemens.cateringportal.io{src}")
    d.rectangle([(0,H-FOOTER_H),(W,H)],fill=BLUE)
    b=d.textbbox((0,0),txt,font=f)
    d.text(((W-(b[2]-b[0]))//2,H-16),txt,font=f,fill=WHITE)

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    now  = datetime.now(timezone.utc)
    local= german_time(now)
    label,kw = kw_label(now)
    out_path = OUT_DIR/f'kantine_{label}.jpg'

    monday   = local - timedelta(days=local.weekday())
    date_str = monday.strftime('%Y-%m-%d')
    url_menu = f"{URL_BASE}/date/{date_str}"
    if _SID: url_menu += f"?ste_sid={_SID}"

    print(f'Target URL : {url_menu}')
    print(f'Week label : {label}  (KW {kw:02d})')

    holiday_map = week_holiday_map(local)
    open_days   = [k for k,v in holiday_map.items() if v is None]
    hol_days    = [k for k,v in holiday_map.items() if v]
    if hol_days:
        print(f'Feiertage: {[(k,holiday_map[k]) for k in hol_days]}')
    print(f'Offene Tage: {open_days}')

    all_keys = day_keys(local)
    print('Scraping...')
    week_data, source = scrape(url_menu, all_keys)
    print(f'Result: {list(week_data.keys())}  source={source}')

    img = render(week_data, kw, label, local, url_menu, holiday_map, source)
    img.save(str(out_path), 'JPEG', quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')

if __name__ == '__main__':
    main()
