#!/usr/bin/env python3
"""Siemens Kantine Regensburg – 800x600 JPEG (Bilderrahmen-Auflösung).

Scraping strategy (DOM-based, not innerText):
  1. warmup(): load base URL, dismiss cookie banner, open the Angular
     mat-menu language picker (globe/language button), then click
     button[value='de-DE'].  Capture localStorage diff and re-inject on
     every subsequent navigation.
  2. scrape_day(): navigate to /date/YYYY-MM-DD, re-inject localStorage,
     wait for .category-header, click the correct day tab, then call
     JS_EXTRACT to pull structured data directly from the DOM.
     Angular renders each product-wrapper TWICE (animation artefact) –
     we deduplicate by (name, intPrice) before returning.
  3. "oder"-alternative shown with prefix "oder" only when an explicit
     "oder"-separator product was scraped; otherwise prefix is "mit".
  4. Fallback chain: DOM-query → innerText line-parser (legacy).
  5. WEEK_OFFSET env var overrides auto-detection:
       auto (default): Mo–Fr<14h → aktuelle Woche (0),
                       Fr≥14h / Sa / So → nächste Woche (1).
     Set WEEK_OFFSET=0 or =1 to force a specific week.
  6. Unknown categories (Vegan, Vegetarisch, Fisch, …) map to their
     semantic target slot (Vegan/Veg.→E2, Fisch→E3); only if that slot is
     already taken do we fall forward to the next free slot.
  7. "wahlweise dazu"-products: the marker product is skipped, the NEXT
     product is stored as 'zusatz' on the main dish.
  8. wrap_text: respects existing hyphens before splitting characters.
  9. render: uniform font size per row (smallest that fits all cells).
 10. Stub labels drawn last as single-line horizontal text (STUB_W=72).
 11. Grid lines start at STUB_W (not x=0) so they don't cut through stubs.
 12. line_height uses +4px padding for better readability.
 13. No "Int:" prefix on prices – employee price is self-evident.
"""
import os, re, json
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ── Config ────────────────────────────────────────────────────────────────────
URL_BASE = os.environ.get("CATERINGPORTAL_URL",
                          "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen")
_SID     = os.environ.get("CATERINGPORTAL_SID", "").strip()
# WEEK_OFFSET: wenn gesetzt, überschreibt die Auto-Logik.
# Auto-Logik: Fr >=14 Uhr / Sa / So → 1 (nächste Woche), sonst 0 (aktuelle Woche).
_WEEK_OFFSET_ENV = os.environ.get("WEEK_OFFSET", "").strip()

FRIDAY_NEXT_WEEK_HOUR = 14  # ab dieser Stunde (Berliner Zeit) am Freitag gilt: nächste Woche

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8
W, H     = 800, 600
FOOTER_H = 20

# ── Colours ───────────────────────────────────────────────────────────────────
BLUE  = (0, 57, 107);    LIGHT = (0, 119, 193)
R_ODD = (240, 246, 252); R_EVEN = (255, 255, 255)
C_VG  = (34, 139, 34);  C_V   = (100, 180, 60)
C_TXT = (30, 30, 30);   WHITE  = (255, 255, 255)
GRID  = (190, 210, 230)
C_HOL_BG  = (220, 220, 220); C_HOL_HDR = (140, 140, 140); C_HOL_TXT = (100, 100, 100)
C_PAST_BG = (235, 235, 235); C_PAST_TXT = (160, 160, 160)
C_TODAY   = (255, 200, 0)
C_TODAY_HDR = (220, 150, 0)
C_ZUSATZ  = (120, 80, 180)

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
    a=y%19;b=y//100;c=y%100;d=b//4;e=b%4;f=(b+8)//25;g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30;i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7
    m=(a+11*h+22*l)//451;mo=(h+l-7*m+114)//31;dy=((h+l-7*m+114)%31)+1
    return date(y,mo,dy)

def bavaria_holidays(y):
    e=_easter(y)
    return {
        date(y,1,1):"Neujahr", date(y,1,6):"Hl. Drei Könige",
        e-timedelta(2):"Karfreitag", e:"Ostersonntag", e+timedelta(1):"Ostermontag",
        date(y,5,1):"Tag der Arbeit", e+timedelta(39):"Christi Himmelfahrt",
        e+timedelta(49):"Pfingstsonntag", e+timedelta(50):"Pfingstmontag",
        e+timedelta(60):"Fronleichnam", date(y,8,15):"Mariä Himmelfahrt",
        date(y,10,3):"Tag der Deutschen Einheit", date(y,11,1):"Allerheiligen",
        date(y,12,25):"1. Weihnachtstag", date(y,12,26):"2. Weihnachtstag",
    }

def week_holiday_map(monday_date):
    y=monday_date.year; hols=bavaria_holidays(y)
    if (monday_date+timedelta(4)).year!=y: hols.update(bavaria_holidays(y+1))
    short=['Mo','Di','Mi','Do','Fr']
    return {f"{short[i]} {(monday_date+timedelta(i)).strftime('%d.%m')}":
            hols.get(monday_date+timedelta(i)) for i in range(5)}

# ── Time helpers ───────────────────────────────────────────────────────────────
def german_time(dt):
    try:
        from zoneinfo import ZoneInfo; return dt.astimezone(ZoneInfo("Europe/Berlin"))
    except ImportError: pass
    import calendar; yr=dt.year
    def last_sun(y,m):
        ld=calendar.monthrange(y,m)[1]; d2=datetime(y,m,ld,tzinfo=timezone.utc)
        return d2-timedelta(days=(d2.weekday()+1)%7)
    cs=last_sun(yr,3).replace(hour=1); ce=last_sun(yr,10).replace(hour=1)
    return dt+timedelta(hours=2 if cs<=dt<ce else 1)

def kw_label(dt):
    d=german_time(dt); y,w,_=d.isocalendar(); return f"{y}-W{w:02d}",int(w)

def day_key(dt_obj):
    return f"{'Mo Di Mi Do Fr'.split()[dt_obj.weekday()]} {dt_obj.strftime('%d.%m')}"

def auto_week_offset(local_dt):
    """Dynamischer WEEK_OFFSET:
    - Sa (5) oder So (6)          → 1 (nächste Woche)
    - Fr (4) und Stunde >= 14     → 1 (nächste Woche)
    - Alles andere                 → 0 (aktuelle Woche)
    """
    wd = local_dt.weekday()  # 0=Mo, 4=Fr, 5=Sa, 6=So
    if wd >= 5:
        return 1
    if wd == 4 and local_dt.hour >= FRIDAY_NEXT_WEEK_HOUR:
        return 1
    return 0

# ── localStorage helpers ───────────────────────────────────────────────────────
_LANG_STORAGE: dict = {}

def _snap(page):
    try:
        raw=page.evaluate("()=>{var o={};for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);o[k]=localStorage.getItem(k);}return JSON.stringify(o);}")
        return json.loads(raw)
    except: return {}

def _inject(page):
    if not _LANG_STORAGE: return
    try:
        page.evaluate("(p)=>{p.forEach(function(x){localStorage.setItem(x[0],x[1]);})}", list(_LANG_STORAGE.items()))
        print(f"    [lang] re-injected {len(_LANG_STORAGE)} localStorage keys")
    except Exception as e:
        print(f"    [lang] inject error: {e}")

# ── Cookie banner ──────────────────────────────────────────────────────────────
def dismiss_cookie(page):
    for sel in [
        "button:has-text('Got it')", "text=Got it!",
        "button:has-text('Accept')", "button:has-text('Akzeptieren')",
        "button:has-text('Alle akzeptieren')",
        "[id*='accept' i]", "[class*='accept' i] button",
        "[id*='cookie' i] button", "[class*='cookie' i] button",
    ]:
        try:
            page.click(sel, timeout=2000)
            print(f"  [cookie] dismissed via {sel!r}")
            page.wait_for_timeout(500); return True
        except: pass
    print("  [cookie] no banner found (OK)")
    return False

# ── Language switch ────────────────────────────────────────────────────────────
def switch_to_german(page):
    global _LANG_STORAGE
    before = _snap(page)

    TRIGGER_SELS = [
        "button[aria-label='language']", "button[aria-label='Language']",
        "button[aria-label*='language' i]", "button[aria-label*='sprache' i]",
        "button:has(img[src*='flags/'])", "button:has(img[src*='en-US'])",
        "button:has(img[alt='English'])", "button:has(img[title='English'])",
        "button:has-text('English')", "button:has-text('EN')",
        "mat-select[aria-label*='lang' i]",
    ]
    DE_SELS = [
        "button[aria-label='Deutsch']", "button[lang='de-DE']",
        "button[value='de-DE']", "[role='menuitem'][aria-label='Deutsch']",
        "[role='menuitem'][lang='de-DE']",
    ]

    def _wait_for_menu_and_click_de():
        OVERLAY_SELS = [".mat-mdc-menu-panel",".mat-menu-panel","[class*='mat-menu']","div[role='menu']"]
        overlay_found = False
        for osel in OVERLAY_SELS:
            try:
                page.wait_for_selector(osel, timeout=3000, state="attached")
                print(f"  [lang] mat-menu overlay found via {osel!r}")
                overlay_found = True; break
            except: pass
        if not overlay_found:
            print("  [lang] mat-menu overlay not detected, trying DE button anyway")
        for sel in DE_SELS:
            try:
                el = page.wait_for_selector(sel, timeout=3000, state="visible")
                if el:
                    el.click(); print(f"  [lang] ✓ clicked DE button via {sel!r}"); return True
            except: pass
        print("  [lang] DE selectors failed, trying JS click on de-DE flag img...")
        result = page.evaluate(r"""
        (function(){
          var img=document.querySelector("img[src*='de-DE'],img[alt='Deutsch'],img[title='Deutsch']");
          if(!img)return 'de-DE img not found';
          var btn=img.closest('button,[role="menuitem"],[role="option"],a')||img.parentElement;
          btn.click();return 'JS-clicked:'+btn.tagName+'|'+(img.src||'');
        })()
        """)
        print(f"  [lang] JS fallback result: {result}")
        return "not found" not in result

    switched = False
    for sel in DE_SELS:
        try:
            el = page.wait_for_selector(sel, timeout=800, state="visible")
            if el:
                el.click(); print(f"  [lang] ✓ DE button already visible, clicked via {sel!r}")
                switched = True; break
        except: pass

    if not switched:
        print("  [lang] DE button not directly visible – opening language trigger...")
        trigger_opened = False
        for tsel in TRIGGER_SELS:
            try:
                page.click(tsel, timeout=2500)
                print(f"  [lang] ✓ trigger clicked via {tsel!r}")
                page.wait_for_timeout(600); trigger_opened = True; break
            except: pass
        if not trigger_opened:
            print("  [lang] no trigger selector matched, trying JS flag-button click...")
            result = page.evaluate(r"""
            (function(){
              var imgs=Array.from(document.querySelectorAll("img[src*='flags/']"));
              for(var img of imgs){
                var btn=img.closest('button,[role="button"]')||img.parentElement;
                if(btn&&btn!==img){btn.click();return 'JS trigger clicked:'+btn.tagName+'|'+(img.src||'');}
              }return 'no flags img found';
            })()
            """)
            print(f"  [lang] JS trigger fallback: {result}")
            page.wait_for_timeout(600)
        switched = _wait_for_menu_and_click_de()

    if not switched:
        print("  [lang] ✗ WARNING: could not switch to German – proceeding in English")
        return False

    page.wait_for_timeout(1000)
    confirmed = False
    for de_text_sel in ["text=Suppe / Vorspeise","text=Suppe","text=Essen 1","h3.category-header","text=Mittagessen"]:
        try:
            page.wait_for_selector(de_text_sel, timeout=3500)
            print(f"  [lang] ✓ German UI confirmed via {de_text_sel!r}")
            confirmed = True; break
        except: pass
    if not confirmed:
        print("  [lang] German UI not yet confirmed (may appear after navigation)")

    after = _snap(page)
    diff  = {k: v for k, v in after.items() if before.get(k) != v}
    if diff:
        _LANG_STORAGE = diff
        print(f"  [lang] localStorage diff captured: {dict(diff)}")
    else:
        _LANG_STORAGE = {k: 'de-DE' for k in [
            'language','locale','lang','i18n','selectedLanguage','appLanguage',
            'selectedLocale','userLanguage','NG_TRANSLATE_LANG_KEY',
        ]}
        _LANG_STORAGE.update({k: 'de' for k in ['language','locale','lang']})
        print(f"  [lang] no localStorage diff – injecting {len(_LANG_STORAGE)} fallback keys")
    return confirmed or switched


def warmup(page, base_url):
    print("[warmup] Loading base URL...")
    try: page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e: print(f"[warmup] goto failed: {e}"); return
    page.wait_for_timeout(1800)
    dismiss_cookie(page)
    switch_to_german(page)
    print(f"[warmup] done. _LANG_STORAGE has {len(_LANG_STORAGE)} keys")


# ── DOM extractor ──────────────────────────────────────────────────────────────
JS_EXTRACT = r"""
(function(){
  var panel=null;
  var panels=Array.from(document.querySelectorAll('mat-tab-nav-panel,[role="tabpanel"],mat-tab-body'));
  for(var p of panels){var s=window.getComputedStyle(p);if(s.display!=='none'&&s.visibility!=='hidden'&&p.offsetHeight>0){panel=p;break;}}
  if(!panel&&panels.length)panel=panels[0];
  var root=panel||document;
  var result=[];
  var categories=Array.from(root.querySelectorAll('app-category,.grid-row'));
  if(!categories.length)categories=Array.from(root.querySelectorAll('h3.category-header')).map(h=>h.parentElement);
  for(var cat of categories){
    var hdr=cat.querySelector('h3.category-header,.category-header,h3');
    var catName=hdr?hdr.textContent.trim():'';
    if(!catName)continue;
    var products=Array.from(cat.querySelectorAll('div.product-wrapper'));
    if(!products.length)products=Array.from(cat.querySelectorAll('[class*="product"]'));
    var catProducts=[];
    for(var prod of products){
      var nameEl=prod.querySelector('span.legacy-text-xxl,span.pre-wrap,button span,.name-column span');
      var rawName=nameEl?nameEl.textContent:'';
      var name=rawName.trim().replace(/\u00a0/g,' ');
      var priceEls=Array.from(prod.querySelectorAll('div.price,.price'));
      var intPrice='',extPrice='';
      for(var pe of priceEls){
        var pt=pe.textContent.replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
        if(/^Int/i.test(pt))intPrice=pt.replace(/^Int\s*/i,'').trim();
        if(/^Ext/i.test(pt))extPrice=pt.replace(/^Ext\s*/i,'').trim();
      }
      catProducts.push({name:name,intPrice:intPrice,extPrice:extPrice});
    }
    result.push({category:catName,products:catProducts});
  }
  return JSON.stringify(result);
})()
"""

# Known fixed mappings
CAT_NORM_FIXED = {
    'suppe / vorspeise':'Suppe','suppe/vorspeise':'Suppe','suppe':'Suppe',
    'soup / starter':'Suppe','soup/starter':'Suppe','soup':'Suppe',
    'essen 1':'Essen 1','food 1':'Essen 1','gericht 1':'Essen 1',
    'essen 2':'Essen 2','food 2':'Essen 2','gericht 2':'Essen 2',
    'essen 3':'Essen 3','food 3':'Essen 3','gericht 3':'Essen 3',
}
CAT_FLEXIBLE = {
    'fisch':'Essen 3','fish':'Essen 3',
    'vegan':'Essen 2','vegane':'Essen 2',
    'vegetarisch':'Essen 2','vegetarian':'Essen 2',
}
_ESSEN_SLOTS = ['Essen 1','Essen 2','Essen 3']

def norm_cat(raw, used_cats=None):
    key = raw.lower().strip()
    if key in CAT_NORM_FIXED:
        return CAT_NORM_FIXED[key]
    if key in CAT_FLEXIBLE:
        preferred = CAT_FLEXIBLE[key]
        if used_cats is None:
            return preferred
        try: start_idx = _ESSEN_SLOTS.index(preferred)
        except ValueError: start_idx = 0
        if preferred not in used_cats:
            return preferred
        for slot in _ESSEN_SLOTS[start_idx + 1:]:
            if slot not in used_cats:
                return slot
        for slot in _ESSEN_SLOTS[:start_idx]:
            if slot not in used_cats:
                return slot
        return preferred
    if used_cats is not None:
        for slot in _ESSEN_SLOTS:
            if slot not in used_cats:
                return slot
    return raw.strip()

def vv_from_name(name):
    low=name.lower()
    if any(w in low for w in ['vegan','vegane','veganer','veganes']): return 'VG'
    if any(w in low for w in ['vegetarian','vegetarisch','vegetarische','vegetarischer','vegetarisches']): return 'V'
    return ''

def vv_from_cat(raw_cat):
    key = raw_cat.lower().strip()
    if key in ('vegan','vegane'): return 'VG'
    if key in ('vegetarisch','vegetarian'): return 'V'
    return ''

def _dedup_products(prods):
    seen=set(); out=[]
    for p in prods:
        key=(p['name'].strip().lower(), p['intPrice'].strip())
        if key not in seen: seen.add(key); out.append(p)
    return out

def _names_differ(a, b):
    def norm(s): return re.sub(r'[\s"\'«»„"]+','',s).lower()
    return norm(a)!=norm(b)

WAHLWEISE_RE = re.compile(r'^wahlweise(\s+dazu)?(\s+|$)', re.IGNORECASE)

def parse_dom_result(raw_json):
    try: data=json.loads(raw_json)
    except Exception as e: print(f"  [parse] JSON error: {e}"); return []
    dishes=[]
    used_cats = set()
    for cat_entry in data:
        raw_cat = cat_entry.get('category','')
        cat = norm_cat(raw_cat, used_cats)
        if not cat:
            print(f"  [parse] skip: {raw_cat!r}")
            continue
        cat_vv = vv_from_cat(raw_cat)
        prods=_dedup_products(cat_entry.get('products',[]))
        print(f"  [parse] {cat!r} (raw:{raw_cat!r}) → {len(cat_entry['products'])} raw → {len(prods)} deduped")
        main_dish=None
        explicit_oder=False
        next_is_zusatz=False

        for p in prods:
            name=p['name'].strip().replace('\n',' / ')

            if WAHLWEISE_RE.match(name):
                remainder = WAHLWEISE_RE.sub('', name).strip()
                if remainder and main_dish is not None:
                    main_dish['zusatz'] = f"wahlweise: {remainder}"
                    main_dish['zusatz_preis'] = p['intPrice']
                    print(f"    [parse] zusatz (inline): {remainder!r} | {p['intPrice']!r}")
                elif main_dish is not None:
                    next_is_zusatz = True
                    print(f"    [parse] zusatz marker found, next product = ingredient")
                continue

            if next_is_zusatz:
                if main_dish is not None:
                    main_dish['zusatz'] = f"wahlweise: {name}"
                    main_dish['zusatz_preis'] = p['intPrice']
                    print(f"    [parse] zusatz (next): {name!r} | {p['intPrice']!r}")
                next_is_zusatz = False
                continue

            if re.match(r'^(oder|or)$', name.strip(), re.IGNORECASE):
                explicit_oder = True
                print(f"    [parse] 'oder' separator")
                continue

            if main_dish is None:
                vv = vv_from_name(name) or cat_vv
                main_dish={'kategorie':cat,'name':name,'preis_int':p['intPrice'],
                           'vv':vv,'oder':'','oder_prefix':'mit','oder_preis':'','oder_vv':'',
                           'zusatz':'','zusatz_preis':''}
                explicit_oder=False
                print(f"    [parse] main: {name!r} | {p['intPrice']!r}")
            elif explicit_oder or main_dish['oder']=='':
                if _names_differ(main_dish['name'],name):
                    main_dish['oder']=name
                    main_dish['oder_prefix']='oder' if explicit_oder else 'mit'
                    main_dish['oder_preis']=p['intPrice']
                    main_dish['oder_vv']=vv_from_name(name)
                    print(f"    [parse] {main_dish['oder_prefix']}: {name!r} vv={main_dish['oder_vv']!r}")
                else:
                    print(f"    [parse] skip dup: {name!r}")
                explicit_oder=False

        if main_dish:
            dishes.append(main_dish)
            used_cats.add(cat)
    return dishes


# ── Fallback: innerText line-parser ──────────────────────────────────────────
INT_PRICE_RE=re.compile(r'Int[\s\u00a0]+[\u20ac$]?([0-9]+[.,][0-9]{2})|Int[\s\u00a0]+([0-9]+[.,][0-9]{2})[\s\u00a0]*[\u20ac$]',re.IGNORECASE)
ALLERGEN_RE=re.compile(r'^[A-Z]{1,10}$')
OR_RE=re.compile(r'^(oder|or)$',re.IGNORECASE)
EXT_RE=re.compile(r'^Ext[\s\u00a0]',re.IGNORECASE)
CAT_HEADERS_FB={'Soup / Starter':'Suppe','Soup/Starter':'Suppe','Soup':'Suppe','Suppe / Vorspeise':'Suppe','Suppe/Vorspeise':'Suppe','Suppe':'Suppe','Food 1':'Essen 1','Food 2':'Essen 2','Food 3':'Essen 3','Essen 1':'Essen 1','Essen 2':'Essen 2','Essen 3':'Essen 3','Gericht 1':'Essen 1','Gericht 2':'Essen 2','Gericht 3':'Essen 3','Fish':'Essen 3','Fisch':'Essen 3','Vegan':'Essen 2','Vegetarisch':'Essen 2'}
NOISE_FB={'Learn more','Got it!','home','Home','view_compact','Menu','place','Stores','Impressum','close','Close','English','Lunch','filter_list','Filter','Store','clear','Info','MyCasinoCard','Opening hours','Nutzungsbedingungen','Datenschutzerklärung','Speiseplan','Mittagessen','Informationen','Deutsch','Mehr erfahren','note','Aktuelle Woche'}

def _norm_price(raw):
    try: return f"{float(raw.replace(',','.')):.2f}".replace('.',',')
    except: return raw

def parse_flat_fallback(lines):
    print("  [fallback-parser] running")
    dishes=[]; cur_cat=None; cur_vv=''; cur_name=[]; seen_cats=set()
    after_oder=False; oder_name=[]
    SLOTS=['Suppe','Essen 1','Essen 2','Essen 3']
    def next_free(slot):
        try: idx=SLOTS.index(slot)
        except: return None
        return next((s for s in SLOTS[idx+1:] if s not in seen_cats),None)
    def flush(price=''):
        nonlocal cur_name,cur_vv,after_oder,oder_name
        toks=[t for t in cur_name if not ALLERGEN_RE.match(t) and not OR_RE.match(t) and t not in NOISE_FB and not INT_PRICE_RE.search(t) and not EXT_RE.match(t)]
        while toks and ALLERGEN_RE.match(toks[-1]): toks.pop()
        name=' '.join(toks).strip()
        if cur_cat and name and len(name)>=3 and cur_cat not in seen_cats:
            dishes.append({'kategorie':cur_cat,'name':name,'preis_int':price+'\u00a0\u20ac' if price else '','vv':cur_vv,'oder':'','oder_prefix':'mit','oder_preis':'','oder_vv':'','zusatz':'','zusatz_preis':''})
            seen_cats.add(cur_cat)
        cur_name=[]; cur_vv=''; after_oder=False; oder_name=[]
    def flush_oder():
        nonlocal after_oder,oder_name
        toks=[t for t in oder_name if not ALLERGEN_RE.match(t) and t not in NOISE_FB and not INT_PRICE_RE.search(t) and not EXT_RE.match(t)]
        while toks and ALLERGEN_RE.match(toks[-1]): toks.pop()
        name=' '.join(toks).strip()
        if name and len(name)>=3:
            for dish in reversed(dishes):
                if dish['kategorie']==cur_cat:
                    if _names_differ(dish['name'],name):
                        dish['oder']=name
                        dish['oder_prefix']='oder'
                        dish['oder_vv']=vv_from_name(name)
                    break
        after_oder=False; oder_name=[]
    VEGAN_LABELS={'Vegan':'VG','Vegetarian':'V','Vegetarisch':'V','vegetarisch':'V'}
    for line in lines:
        line=line.strip()
        if not line or line in NOISE_FB: continue
        if line in CAT_HEADERS_FB:
            if after_oder: flush_oder()
            flush(); cur_cat=CAT_HEADERS_FB[line]; cur_name=[]; cur_vv=''; continue
        if cur_cat is None: continue
        if line in VEGAN_LABELS:
            if after_oder: flush_oder()
            flush(); nxt=next_free(cur_cat)
            if nxt: cur_cat=nxt
            cur_vv=VEGAN_LABELS[line]; cur_name=[]; continue
        m=INT_PRICE_RE.search(line)
        if m:
            if after_oder: flush_oder()
            else: flush(_norm_price(m.group(1) or m.group(2) or ''))
            continue
        if OR_RE.match(line):
            if after_oder: flush_oder()
            after_oder=True; oder_name=[]; continue
        if EXT_RE.match(line) or ALLERGEN_RE.match(line): continue
        if after_oder: oder_name.append(line)
        else: cur_name.append(line)
    if after_oder: flush_oder()
    flush()
    return dishes


# ── JS helpers ─────────────────────────────────────────────────────────────────
JS_CLICK_TAB=r"""
(function(dateStr){
  var tabs=Array.from(document.querySelectorAll('[role="tab"],.mdc-tab,.mat-tab-label,.mat-mdc-tab'));
  var all=Array.from(document.querySelectorAll('button,a,div,span,li')).filter(el=>{var t=(el.innerText||el.textContent||'').trim();return t.includes(dateStr)&&t.length<40;});
  var target=tabs.concat(all).find(el=>(el.innerText||el.textContent||'').trim().includes(dateStr));
  if(target){target.click();return(target.innerText||target.textContent||'').trim().slice(0,60);}return null;
})
"""
JS_TAB_TEXT=r"""
(function(){var panels=Array.from(document.querySelectorAll('mat-tab-nav-panel,[role="tabpanel"],mat-tab-body'));var active=panels.find(p=>{var s=window.getComputedStyle(p);return s.display!=='none'&&s.visibility!=='hidden'&&p.offsetHeight>0;})||panels[0];if(!active){var b=document.body.cloneNode(true);b.querySelectorAll('script,style,noscript').forEach(e=>e.remove());return b.innerText||b.textContent||'';}return active.innerText||active.textContent||'';}())
"""

# ── Scrape one day ────────────────────────────────────────────────────────────
def scrape_day(page, date_obj):
    url=f"{URL_BASE}/date/{date_obj.str