#!/usr/bin/env python3
"""Siemens Kantine Regensburg – 1200x800 JPEG.

Portal behaviour:
  - Cookie consent banner must be dismissed first.
  - Language is switched by clicking the German flag button whose src
    contains '/assets/icons/flags/de-DE.svg'.
  - /date/YYYY-MM-DD does NOT auto-switch the visible tab – must click it.
  - After tab click we poll until panel content changes (Angular re-render).
"""
import os, re
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ── Config ────────────────────────────────────────────────────────────────────
URL_BASE = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID = os.environ.get("CATERINGPORTAL_SID", "").strip()

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8
W, H     = 1200, 800
FOOTER_H = 26

# ── Colours ───────────────────────────────────────────────────────────────────
BLUE  = (0, 57, 107);    LIGHT = (0, 119, 193)
R_ODD = (240, 246, 252); R_EVEN = (255, 255, 255)
C_VG  = (34, 139, 34);  C_V   = (100, 180, 60)
C_TXT = (30, 30, 30);   WHITE  = (255, 255, 255)
GRID  = (190, 210, 230)
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

# ── Cookie banner + German language setup ────────────────────────────────
def dismiss_cookie_banner(page):
    """Try all known cookie consent selectors. Silent if none found."""
    for sel in [
        "text=Got it!",
        "button:has-text('Got it')",
        "button:has-text('Accept')",
        "button:has-text('Akzeptieren')",
        "button:has-text('Alle akzeptieren')",
        "[id*='accept' i]",
        "[class*='accept' i] button",
        "[id*='cookie' i] button",
        "[class*='cookie' i] button",
        "[id*='consent' i] button",
    ]:
        try:
            page.click(sel, timeout=2000)
            print(f"  [cookie] dismissed: {sel!r}")
            page.wait_for_timeout(600)
            return True
        except Exception:
            pass
    return False


def switch_to_german(page):
    """
    Click the German flag button. The portal uses img src
    '/assets/icons/flags/de-DE.svg' as the language switcher.
    After clicking, wait for a German-language selector to confirm.
    """
    FLAG_SEL = "img[src*='de-DE']"
    # The img may be inside a button or anchor – try the image itself
    # and also its parent button/a.
    JS_CLICK_FLAG = r"""
    (function(){
      var img = document.querySelector("img[src*='de-DE']");
      if (!img) return 'not found';
      var btn = img.closest('button,a') || img;
      btn.click();
      return btn.tagName + ' clicked: ' + (img.getAttribute('src') || '');
    })()
    """
    try:
        # Wait for the flag image to appear in the DOM
        page.wait_for_selector(FLAG_SEL, timeout=8000)
        result = page.evaluate(JS_CLICK_FLAG)
        print(f"  [lang] flag click: {result}")
        # Wait for Angular to re-render in German
        for de_sel in ["text=Suppe", "text=Suppe / Vorspeise", "text=Essen 1",
                       "text=Do.", "text=Fr.", "text=Mo."]:
            try:
                page.wait_for_selector(de_sel, timeout=4000)
                print(f"  [lang] German confirmed: {de_sel!r}")
                return True
            except Exception:
                pass
        print("  [lang] flag clicked but German selector not confirmed")
        return False
    except Exception as e:
        print(f"  [lang] flag button not found: {e}")
        return False


def warmup(page, base_url):
    """Load the base URL, dismiss cookie banner, switch to German."""
    print("  [warmup] loading base URL...")
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=40000)
    except Exception as e:
        print(f"  [warmup] goto failed: {e}")
        return
    page.wait_for_timeout(1500)   # let Angular bootstrap
    dismiss_cookie_banner(page)
    switch_to_german(page)


# ── Parser ────────────────────────────────────────────────────────────────────
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
    'Fish':     'Essen 3',
    'Fisch':    'Essen 3',
}

VEGAN_LABELS = {
    'Vegan':        'VG',
    'Vegetarian':   'V',
    'Vegetarisch':  'V',
    'vegetarisch':  'V',
}

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

def _norm_price(raw):
    try: return f"{float(raw.replace(',','.')):.2f}".replace('.',',')
    except ValueError: return raw

def parse_flat(lines):
    dishes=[]; cur_cat=None; cur_vv=''; cur_name=[]; seen_cats=set()
    SLOTS=['Suppe','Essen 1','Essen 2','Essen 3']

    def next_free(slot):
        try: idx=SLOTS.index(slot)
        except ValueError: return None
        return next((s for s in SLOTS[idx+1:] if s not in seen_cats), None)

    def flush(price=''):
        nonlocal cur_name, cur_vv
        toks=[t for t in cur_name
              if not ALLERGEN_RE.match(t) and not OR_RE.match(t)
              and t not in NOISE and not INT_PRICE_RE.search(t)
              and not EXT_RE.match(t)]
        while toks and ALLERGEN_RE.match(toks[-1]): toks.pop()
        name=' '.join(toks).strip()
        if cur_cat and name and len(name)>=3 and cur_cat not in seen_cats:
            dishes.append({'kategorie':cur_cat,'name':name,
                           'preis_int':price+'\u00a0\u20ac' if price else '','vv':cur_vv})
            seen_cats.add(cur_cat)
        cur_name=[]; cur_vv=''

    for line in lines:
        line=line.strip()
        if not line or line in NOISE: continue
        if line in CAT_HEADERS:
            flush(); cur_cat=CAT_HEADERS[line]; cur_name=[]; cur_vv=''; continue
        if cur_cat is None: continue
        if line in VEGAN_LABELS:
            flush()
            nxt=next_free(cur_cat)
            if nxt: cur_cat=nxt
            cur_vv=VEGAN_LABELS[line]; cur_name=[]; continue
        m=INT_PRICE_RE.search(line)
        if m: flush(_norm_price(m.group(1) or m.group(2) or '')); continue
        if OR_RE.match(line): seen_cats.add(cur_cat); cur_name=[]; cur_vv=''; continue
        if EXT_RE.match(line) or ALLERGEN_RE.match(line): continue
        cur_name.append(line)

    flush()
    return dishes


# ── JS helpers ─────────────────────────────────────────────────────────────────
JS_TAB_TEXT = r"""
(function(){
  var panels=Array.from(document.querySelectorAll(
    'mat-tab-body,[role="tabpanel"],.mat-tab-body'));
  var active=panels.find(p=>{
    var s=window.getComputedStyle(p);
    return s.display!=='none'&&s.visibility!=='hidden'&&p.offsetHeight>0;
  })||panels[0];
  if(!active){
    var b=document.body.cloneNode(true);
    b.querySelectorAll('script,style,noscript').forEach(e=>e.remove());
    return b.innerText||b.textContent||'';
  }
  return active.innerText||active.textContent||'';
})()
"""

JS_CLICK_TAB = r"""
(function(dateStr){
  var tabs=Array.from(document.querySelectorAll(
    '[role="tab"],mat-tab-header .mdc-tab,mat-tab-header .mat-tab-label,.mat-mdc-tab,.mat-tab-label-content'));
  var all=Array.from(document.querySelectorAll('*')).filter(el=>{
    if(!['BUTTON','A','DIV','SPAN','LI'].includes(el.tagName))return false;
    var t=(el.innerText||el.textContent||'').trim();
    return t.includes(dateStr)&&t.length<30;
  });
  var target=tabs.concat(all).find(el=>(el.innerText||el.textContent||'').trim().includes(dateStr));
  if(target){target.click();return(target.innerText||target.textContent||'').trim();}
  return null;
})
"""

def get_tab_text(page):
    raw=page.evaluate(JS_TAB_TEXT) or ''
    return [l.strip() for l in raw.splitlines() if l.strip()]


# ── Scrape one day ────────────────────────────────────────────────────────────
def scrape_day(page, date_obj):
    url = f"{URL_BASE}/date/{date_obj.strftime('%Y-%m-%d')}"
    if _SID: url += f"?ste_sid={_SID}"
    date_label = date_obj.strftime('%d.%m')
    print(f"  Loading {url}  [{date_label}]")

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)

    # Dismiss cookie banner in case it re-appears
    dismiss_cookie_banner(page)

    # Wait for any category header
    found_sel = None
    for sel in [
        "text=Suppe / Vorspeise", "text=Suppe", "text=Essen 1",
        "text=Soup / Starter", "text=Food 1",
    ]:
        try:
            page.wait_for_selector(sel, timeout=12000)
            found_sel = sel
            break
        except Exception:
            pass
    if not found_sel:
        print("    no menu selector found, extra wait 5s")
        page.wait_for_timeout(5000)
    else:
        page.wait_for_timeout(400)

    # Click the correct day tab and wait for Angular re-render
    before_sig = ' '.join(get_tab_text(page)[:6])
    clicked    = page.evaluate(f"({JS_CLICK_TAB})('{date_label}')")
    print(f"    tab click: {clicked!r}")

    if clicked:
        for _ in range(20):
            page.wait_for_timeout(200)
            if ' '.join(get_tab_text(page)[:6]) != before_sig:
                print("    content changed")
                break
        else:
            print("    content unchanged (same tab?)")
    else:
        print(f"    no tab for {date_label}, using current content")
        page.wait_for_timeout(600)

    lines = get_tab_text(page)
    print(f"    lines: {len(lines)}")
    for i, l in enumerate(lines[:30]):
        print(f"      {i:2d}: {l[:110]}")

    dishes = parse_flat(lines)
    print(f"    -> {[(d['kategorie'], d['vv'], d['name'][:22]) for d in dishes]}")
    return dishes


# ── Render ────────────────────────────────────────────────────────────────────
def wrap_text(draw, text, f, max_w, max_lines=4):
    out, cur = [], ''
    for w in text.split():
        t=(cur+' '+w).strip() if cur else w
        b=draw.textbbox((0,0),t,font=f)
        if b[2]-b[0]<=max_w: cur=t
        else:
            if cur: out.append(cur)
            cur=w
        if len(out)>=max_lines-1 and cur: break
    if cur: out.append(cur)
    return out[:max_lines]

CATS=['Suppe','Essen 1','Essen 2','Essen 3']
CAT_LABEL={'Suppe':'Suppe','Essen 1':'Essen 1','Essen 2':'Essen 2','Essen 3':'Essen 3'}

def render(week_data,kw,label,local_dt,url_menu,holiday_map,today_date,source=''):
    img=Image.new('RGB',(W,H),(255,255,255))
    d=ImageDraw.Draw(img)
    ftit=lf(18,True);fday=lf(13,True);ftxt=lf(12)
    fbdg=lf(10,True);fprc=lf(10);fftr=lf(10);fstb=lf(10,True)
    HDR_H=44;DAY_H=26;LEGEND_H=20;STUB_W=52

    d.rectangle([(0,0),(W,HDR_H)],fill=BLUE)
    title=f'Siemens Kantine Regensburg  \u2502  KW {kw:02d}'
    b=d.textbbox((0,0),title,font=ftit)
    d.text(((W-(b[2]-b[0]))//2,(HDR_H-(b[3]-b[1]))//2),title,font=ftit,fill=WHITE)
    y=HDR_H

    all_days=list(holiday_map.keys())
    dw=(W-STUB_W)//len(all_days)

    d.rectangle([(0,y),(STUB_W-1,y+DAY_H-1)],fill=BLUE)
    for i,day in enumerate(all_days):
        x=STUB_W+i*dw
        is_hol=holiday_map[day] is not None
        is_past=_is_past(day,today_date)
        col=C_HOL_HDR if is_hol else (C_PAST_TXT if is_past else LIGHT)
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)],fill=col)
        b=d.textbbox((0,0),day,font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2,y+(DAY_H-(b[3]-b[1]))//2),day,font=fday,fill=WHITE)
        d.line([(x,y),(x,y+DAY_H)],fill=BLUE,width=1)
    y+=DAY_H

    avail=H-y-FOOTER_H-LEGEND_H-4
    rs=int(avail*0.18); re_=(avail-rs)//3
    ROW_H={'Suppe':rs,'Essen 1':re_,'Essen 2':re_,'Essen 3':avail-rs-2*re_}

    for ri,cat in enumerate(CATS):
        rh=ROW_H[cat]
        d.line([(0,y),(W,y)],fill=GRID,width=1)
        d.rectangle([(0,y),(STUB_W-1,y+rh-1)],fill=BLUE)
        lbl=CAT_LABEL[cat]
        b=d.textbbox((0,0),lbl,font=fstb)
        tmp=Image.new('RGBA',(b[3]-b[1]+4,b[2]-b[0]+4),(0,0,0,0))
        td=ImageDraw.Draw(tmp); td.text((2,2),lbl,font=fstb,fill=WHITE)
        tmp_r=tmp.rotate(90,expand=True)
        img.paste(tmp_r,(max(0,(STUB_W-tmp_r.width)//2),max(y,y+(rh-tmp_r.height)//2)),tmp_r)

        for i,day in enumerate(all_days):
            x=STUB_W+i*dw
            is_hol=holiday_map[day] is not None
            is_past=_is_past(day,today_date)
            bg=C_PAST_BG if is_past else (C_HOL_BG if is_hol else (R_ODD if ri%2==0 else R_EVEN))
            d.rectangle([(x,y),(x+dw-1,y+rh-1)],fill=bg)
            d.line([(x,y),(x,y+rh)],fill=GRID,width=1)

            if is_past:
                if ri==0:
                    b=d.textbbox((0,0),'vergangen',font=fprc)
                    d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-6),'vergangen',font=fprc,fill=C_PAST_TXT)
                continue
            if is_hol:
                if ri==0:
                    hn=holiday_map[day]
                    b=d.textbbox((0,0),'Feiertag',font=fbdg)
                    bw=b[2]-b[0]+8;bh=b[3]-b[1]+5
                    bx=x+(dw-bw)//2;by=y+8
                    d.rounded_rectangle([(bx,by),(bx+bw,by+bh)],radius=4,fill=(160,160,160))
                    d.text((bx+4,by+2),'Feiertag',font=fbdg,fill=WHITE)
                    cy=by+bh+5
                    for ln in wrap_text(d,hn,ftxt,dw-8,3):
                        b2=d.textbbox((0,0),ln,font=ftxt)
                        d.text((x+(dw-(b2[2]-b2[0]))//2,cy),ln,font=ftxt,fill=C_HOL_TXT);cy+=15
                continue

            PAD=6
            items=[it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items:
                b=d.textbbox((0,0),'–',font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-7),'–',font=ftxt,fill=(180,180,180))
                continue
            it=items[0];cx=x+PAD;cy=y+PAD;avw=dw-2*PAD
            if it['vv']:
                bl='Vegan' if it['vv']=='VG' else 'Veg.'
                bc=C_VG if it['vv']=='VG' else C_V
                b=d.textbbox((0,0),bl,font=fbdg)
                bw=b[2]-b[0]+7;bh2=b[3]-b[1]+4
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh2)],radius=3,fill=bc)
                d.text((cx+4,cy+2),bl,font=fbdg,fill=WHITE);cy+=bh2+3
            max_ln=max(2,min(5,(rh-cy+y-16)//14))
            for ln in wrap_text(d,it['name'],ftxt,avw,max_ln):
                d.text((cx,cy),ln,font=ftxt,fill=C_TXT);cy+=15
            if it['preis_int']:
                pl=f"Int: {it['preis_int']}"
                b=d.textbbox((0,0),pl,font=fprc)
                d.text((x+dw-(b[2]-b[0])-PAD,y+rh-(b[3]-b[1])-4),pl,font=fprc,fill=LIGHT)
        y+=rh

    d.line([(0,y),(W,y)],fill=GRID,width=1);y+=1
    d.rectangle([(0,y),(W,y+LEGEND_H)],fill=(245,249,253))
    lx=8
    for col,txt in [(C_VG,'Vegan'),(C_V,'Vegetarisch'),(C_HOL_HDR,'Feiertag'),(C_PAST_BG,'vergangen')]:
        d.rectangle([(lx,y+5),(lx+14,y+14)],fill=col)
        b=d.textbbox((0,0),txt,font=fprc)
        d.text((lx+18,y+3),txt,font=fprc,fill=C_TXT);lx+=18+(b[2]-b[0])+18
    d.text((lx,y+3),'Int = Mitarbeiterpreis',font=fprc,fill=(120,120,120))
    _footer(d,kw,label,local_dt,fftr,source)
    return img

def _is_past(day_key_str,today_date):
    try:
        dm=day_key_str.split(' ')[1].split('.')
        return date(today_date.year,int(dm[1]),int(dm[0]))<today_date
    except Exception: return False

def _footer(d,kw,label,local_dt,f,source=''):
    src=f' \u2013 {source}' if source else ''
    txt=(f'KW {kw:02d} / {label}  \u2013  '
         f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  \u2013  "
         f"siemens.cateringportal.io{src}")
    d.rectangle([(0,H-FOOTER_H),(W,H)],fill=BLUE)
    b=d.textbbox((0,0),txt,font=f)
    d.text(((W-(b[2]-b[0]))//2,H-18),txt,font=f,fill=WHITE)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now=datetime.now(timezone.utc)
    local=german_time(now)
    label,kw=kw_label(now)
    today_date=local.date()
    out_path=OUT_DIR/f'kantine_{label}.jpg'

    print(f'Week label : {label}  (KW {kw:02d})')
    print(f'Today      : {today_date}')

    holiday_map=week_holiday_map(local)
    hol_days=[k for k,v in holiday_map.items() if v]
    scrape_dates=[
        d for d in week_dates(local)
        if d.date()>=today_date and day_key(d) not in hol_days
    ]
    print(f'Feiertage  : {[(k,holiday_map[k]) for k in hol_days] or "keine"}')
    print(f'Scraping   : {[day_key(d) for d in scrape_dates]}')

    week_data={}
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(
            viewport={"width":1400,"height":900},
            extra_http_headers={"Accept-Language":"de-DE,de;q=0.9,en;q=0.1"},
        )
        warmup(page, URL_BASE)   # cookie banner + German flag button
        for date_obj in scrape_dates:
            dk=day_key(date_obj)
            dishes=scrape_day(page,date_obj)
            if dishes: week_data[dk]=dishes
        browser.close()

    days_filled=len(week_data); days_avail=len(scrape_dates)
    print(f'Ergebnis   : {list(week_data.keys())}  ({days_filled}/{days_avail})')

    img=render(week_data,kw,label,local,URL_BASE,holiday_map,today_date,
               f'DOM ({days_filled}/{days_avail} Tage)')
    img.save(str(out_path),'JPEG',quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')

if __name__=='__main__':
    main()
