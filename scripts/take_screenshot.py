#!/usr/bin/env python3
"""Siemens Kantine Regensburg – 1200x800 JPEG.

Scraping strategy (DOM-based, not innerText):
  1. warmup(): load base URL, dismiss cookie banner, click DE language button
     (button[value='de-DE'] or button[aria-label='Deutsch']), capture
     localStorage diff and re-inject on every subsequent navigation.
  2. scrape_day(): navigate to /date/YYYY-MM-DD, re-inject localStorage,
     wait for .category-header, click the correct day tab, then call
     JS_EXTRACT to pull structured data directly from the DOM:
       - h3.category-header  → category name
       - div.product-wrapper → one product each
           span.legacy-text-xxl → name (may contain \n for side dishes)
           'oder' name + no price → alternative label
           div.price           → Int / Ext prices
  3. Fallback chain: DOM-query → innerText line-parser (legacy).
"""
import os, re, json
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
C_HOL_BG  = (220, 220, 220); C_HOL_HDR = (140, 140, 140); C_HOL_TXT = (100, 100, 100)
C_PAST_BG = (235, 235, 235); C_PAST_TXT = (160, 160, 160)

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
        date(y,1,1):"Neujahr", date(y,1,6):"Hl. Drei K\u00f6nige",
        e-timedelta(2):"Karfreitag", e:"Ostersonntag", e+timedelta(1):"Ostermontag",
        date(y,5,1):"Tag der Arbeit", e+timedelta(39):"Christi Himmelfahrt",
        e+timedelta(49):"Pfingstsonntag", e+timedelta(50):"Pfingstmontag",
        e+timedelta(60):"Fronleichnam", date(y,8,15):"Mari\u00e4 Himmelfahrt",
        date(y,10,3):"Tag der Deutschen Einheit", date(y,11,1):"Allerheiligen",
        date(y,12,25):"1. Weihnachtstag", date(y,12,26):"2. Weihnachtstag",
    }

def week_holiday_map(local_dt):
    monday=local_dt.date()-timedelta(days=local_dt.weekday()); y=monday.year
    hols=bavaria_holidays(y)
    if (monday+timedelta(4)).year!=y: hols.update(bavaria_holidays(y+1))
    short=['Mo','Di','Mi','Do','Fr']
    return {f"{short[i]} {(monday+timedelta(i)).strftime('%d.%m')}":hols.get(monday+timedelta(i)) for i in range(5)}

# ── Time helpers ──────────────────────────────────────────────────────────────
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

def week_dates(local_dt):
    monday=local_dt-timedelta(days=local_dt.weekday())
    return [monday+timedelta(i) for i in range(5)]

def day_key(dt_obj):
    return f"{'Mo Di Mi Do Fr'.split()[dt_obj.weekday()]} {dt_obj.strftime('%d.%m')}"

# ── localStorage helpers ─────────────────────────────────────────────────────────
_LANG_STORAGE: dict = {}

def _snap(page):
    try:
        raw=page.evaluate("()=>{var o={};for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);o[k]=localStorage.getItem(k);}return JSON.stringify(o);}")
        return json.loads(raw)
    except: return {}

def _inject(page):
    if not _LANG_STORAGE: return
    try:
        page.evaluate("(p)=>{p.forEach(function(x){localStorage.setItem(x[0],x[1]);})}",
                      list(_LANG_STORAGE.items()))
        print(f"    [lang] re-injected {len(_LANG_STORAGE)} localStorage keys")
    except Exception as e:
        print(f"    [lang] inject error: {e}")

# ── Cookie banner ───────────────────────────────────────────────────────────────
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
    print("  [cookie] no banner found (OK if already dismissed)")
    return False

# ── Language switch ──────────────────────────────────────────────────────────────
def switch_to_german(page):
    global _LANG_STORAGE
    before = _snap(page)

    # The language selector may be inside a mat-menu that opens on click.
    # Strategy: open the menu first (language toggle button), then click DE.
    # Selectors in priority order:
    LANG_BTN_SELECTORS = [
        # Direct DE button (already open menu)
        "button[value='de-DE']",
        "button[aria-label='Deutsch']",
        "button[lang='de-DE']",
        "[aria-label='Deutsch']",
        # Flag image parent
        "img[src*='de-DE']",
    ]
    # Triggers that open the language dropdown
    MENU_TRIGGER_SELECTORS = [
        "button:has-text('English')",
        "button[aria-label='language']",
        "button:has-text('EN')",
        "[aria-label*='language' i]",
        "[aria-label*='sprache' i]",
        "mat-select[aria-label*='lang' i]",
    ]

    def _try_click_de():
        for sel in LANG_BTN_SELECTORS:
            try:
                el = page.wait_for_selector(sel, timeout=3000)
                if el:
                    # If it's an img, click its parent button/a
                    tag = page.evaluate("(el)=>el.tagName", el)
                    if tag == 'IMG':
                        page.evaluate("(el)=>{var b=el.closest('button,a')||el;b.click();}", el)
                    else:
                        el.click()
                    print(f"  [lang] clicked DE selector: {sel!r}")
                    return True
            except: pass
        return False

    # First try direct click (menu may already be open)
    if not _try_click_de():
        print("  [lang] DE button not visible, trying to open language menu first...")
        for tsel in MENU_TRIGGER_SELECTORS:
            try:
                page.click(tsel, timeout=2500)
                print(f"  [lang] opened language menu via {tsel!r}")
                page.wait_for_timeout(600)
                break
            except: pass
        if not _try_click_de():
            print("  [lang] WARNING: could not click DE button – scraping may be in English")
            return False

    page.wait_for_timeout(1200)
    after = _snap(page)
    diff  = {k:v for k,v in after.items() if before.get(k)!=v}
    if diff:
        _LANG_STORAGE = diff
        print(f"  [lang] localStorage diff keys: {list(diff.keys())}")
        for k,v in diff.items():
            print(f"         {k} = {v!r}")
    else:
        # Fallback: store known language key candidates
        _LANG_STORAGE = {k:'de' for k in
            ['language','locale','lang','i18n','selectedLanguage','appLanguage',
             'selectedLocale','userLanguage','NG_TRANSLATE_LANG_KEY']}
        print("  [lang] no localStorage diff – injecting fallback keys")

    # Confirm German UI
    for de_sel in ["text=Suppe / Vorspeise","text=Suppe","text=Essen 1",
                   "h3.category-header"]:
        try:
            page.wait_for_selector(de_sel, timeout=3000)
            print(f"  [lang] German UI confirmed via {de_sel!r}"); return True
        except: pass
    print("  [lang] German UI not yet confirmed (may appear after navigation)")
    return False


def warmup(page, base_url):
    print("[warmup] Loading base URL...")
    try:
        page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[warmup] goto failed: {e}"); return
    page.wait_for_timeout(1800)   # Angular bootstrap
    dismiss_cookie(page)
    switch_to_german(page)
    print(f"[warmup] done. _LANG_STORAGE has {len(_LANG_STORAGE)} keys")


# ── DOM extractor (primary parser) ──────────────────────────────────────────────
JS_EXTRACT = r"""
(function(){
  // Find active tab panel
  var panel = null;
  var panels = Array.from(document.querySelectorAll(
    'mat-tab-nav-panel, [role="tabpanel"], mat-tab-body'
  ));
  // prefer visible one
  for (var p of panels) {
    var s = window.getComputedStyle(p);
    if (s.display !== 'none' && s.visibility !== 'hidden' && p.offsetHeight > 0) {
      panel = p; break;
    }
  }
  if (!panel && panels.length) panel = panels[0];
  var root = panel || document;

  var result = [];
  var categories = Array.from(root.querySelectorAll('app-category, .grid-row'));
  if (!categories.length) {
    // fallback: find all category headers directly
    categories = Array.from(root.querySelectorAll('h3.category-header')).map(h=>h.parentElement);
  }

  for (var cat of categories) {
    var hdr = cat.querySelector('h3.category-header, .category-header, h3');
    var catName = hdr ? hdr.textContent.trim() : '';
    if (!catName) continue;

    var products = Array.from(cat.querySelectorAll('div.product-wrapper, app-product-list > div > div'));
    // fallback: any product wrapper
    if (!products.length) {
      products = Array.from(cat.querySelectorAll('[class*="product"]'));
    }

    var catProducts = [];
    for (var prod of products) {
      // Name: span.legacy-text-xxl or span.pre-wrap or button > span
      var nameEl = prod.querySelector(
        'span.legacy-text-xxl, span.pre-wrap, button span, .name-column span'
      );
      var rawName = nameEl ? nameEl.textContent : '';
      var name = rawName.trim().replace(/\u00a0/g,' ');

      // Prices: all div.price inside this product
      var priceEls = Array.from(prod.querySelectorAll('div.price, .price'));
      var intPrice = '', extPrice = '';
      for (var pe of priceEls) {
        var pt = pe.textContent.replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim();
        if (/^Int/i.test(pt))  intPrice  = pt.replace(/^Int\s*/i,'').trim();
        if (/^Ext/i.test(pt))  extPrice  = pt.replace(/^Ext\s*/i,'').trim();
      }

      // Allergens
      var allergenEls = Array.from(prod.querySelectorAll(
        'app-product-label-list span, .allergen-column span, .label-list span'
      ));
      var allergens = allergenEls.map(e=>e.textContent.trim()).filter(Boolean);

      catProducts.push({name:name, intPrice:intPrice, extPrice:extPrice,
                        allergens:allergens});
    }
    result.push({category:catName, products:catProducts});
  }
  return JSON.stringify(result);
})()
"""

# ── Category name normaliser ────────────────────────────────────────────────────────
CAT_NORM = {
    'suppe / vorspeise':'Suppe','suppe/vorspeise':'Suppe','suppe':'Suppe',
    'soup / starter':'Suppe','soup/starter':'Suppe','soup':'Suppe',
    'essen 1':'Essen 1','food 1':'Essen 1','gericht 1':'Essen 1',
    'essen 2':'Essen 2','food 2':'Essen 2','gericht 2':'Essen 2',
    'essen 3':'Essen 3','food 3':'Essen 3','gericht 3':'Essen 3',
    'fisch':'Essen 3','fish':'Essen 3',
}

def norm_cat(raw):
    return CAT_NORM.get(raw.lower().strip(), raw.strip())

VEGAN_WORDS = {'vegan','vegane','veganer','veganes','vegetarian','vegetarisch',
               'vegetarische','vegetarischer','vegetarisches'}

def vv_from_name(name):
    low = name.lower()
    if any(w in low for w in ['vegan','vegane','veganer','veganes']): return 'VG'
    if any(w in low for w in ['vegetarian','vegetarisch','vegetarische',
                              'vegetarischer','vegetarisches']): return 'V'
    return ''

def parse_dom_result(raw_json):
    """
    Convert JS_EXTRACT output to list of dish dicts:
    {kategorie, name, preis_int, vv, oder}
    """
    try:
        data = json.loads(raw_json)
    except Exception as e:
        print(f"  [parse] JSON decode error: {e}")
        return []

    dishes = []
    for cat_entry in data:
        cat = norm_cat(cat_entry.get('category',''))
        if not cat:
            print(f"  [parse] skipping unknown category: {cat_entry.get('category')!r}")
            continue
        print(f"  [parse] category {cat!r} → {len(cat_entry['products'])} products")

        prods = cat_entry.get('products', [])
        main_dish = None

        for p in prods:
            name = p['name'].replace('\n', ' ').strip()
            # Multiline names: keep newlines as ' / ' for side dishes
            name_display = p['name'].strip().replace('\n', ' / ')
            int_price = p['intPrice']
            is_oder = (name.lower() == 'oder' or name.lower() == 'or')

            if is_oder:
                print(f"    [parse] 'oder' separator found")
                continue   # next product is the alternative

            if main_dish is None:
                # First product in this category = main dish
                main_dish = {
                    'kategorie': cat,
                    'name':      name_display,
                    'preis_int': int_price,
                    'vv':        vv_from_name(name_display),
                    'oder':      '',
                    'oder_preis': '',
                }
                print(f"    [parse] main: {name_display!r} | Int:{int_price!r}")
            else:
                # Subsequent product = 'oder'-alternative
                # (the 'oder' separator product was already skipped above
                #  but even without it the logic is the same)
                main_dish['oder']       = name_display
                main_dish['oder_preis'] = int_price
                print(f"    [parse] oder: {name_display!r} | Int:{int_price!r}")

        if main_dish:
            dishes.append(main_dish)

    return dishes


# ── Fallback: innerText line-parser (legacy) ─────────────────────────────────────
INT_PRICE_RE = re.compile(
    r'Int[\s\u00a0]+[\u20ac$]?([0-9]+[.,][0-9]{2})'
    r'|Int[\s\u00a0]+([0-9]+[.,][0-9]{2})[\s\u00a0]*[\u20ac$]',
    re.IGNORECASE
)
ALLERGEN_RE = re.compile(r'^[A-Z]{1,10}$')
OR_RE       = re.compile(r'^(oder|or)$', re.IGNORECASE)
EXT_RE      = re.compile(r'^Ext[\s\u00a0]', re.IGNORECASE)

CAT_HEADERS_FB = {
    'Soup / Starter':'Suppe','Soup/Starter':'Suppe','Soup':'Suppe',
    'Suppe / Vorspeise':'Suppe','Suppe/Vorspeise':'Suppe','Suppe':'Suppe',
    'Food 1':'Essen 1','Food 2':'Essen 2','Food 3':'Essen 3',
    'Essen 1':'Essen 1','Essen 2':'Essen 2','Essen 3':'Essen 3',
    'Gericht 1':'Essen 1','Gericht 2':'Essen 2','Gericht 3':'Essen 3',
    'Fish':'Essen 3','Fisch':'Essen 3',
}
NOISE_FB = {
    'Learn more','Got it!','home','Home','view_compact','Menu','place',
    'Stores','Impressum','close','Close','English','Lunch','filter_list',
    'Filter','Store','clear','Info','MyCasinoCard','Opening hours','edit',
    'QR Code','Next','Previous','Nutzungsbedingungen','Datenschutzerkl\u00e4rung',
    'Speiseplan','Mittagessen','Informationen','Deutsch','Mehr erfahren',
    'note','Aktuelle Woche',
}

def _norm_price(raw):
    try: return f"{float(raw.replace(',','.')):.2f}".replace('.',',')
    except: return raw

def parse_flat_fallback(lines):
    print("  [fallback-parser] running innerText line parser")
    dishes=[]; cur_cat=None; cur_vv=''; cur_name=[]; seen_cats=set()
    after_oder=False; oder_name=[]
    SLOTS=['Suppe','Essen 1','Essen 2','Essen 3']

    def next_free(slot):
        try: idx=SLOTS.index(slot)
        except: return None
        return next((s for s in SLOTS[idx+1:] if s not in seen_cats),None)

    def flush(price=''):
        nonlocal cur_name,cur_vv,after_oder,oder_name
        toks=[t for t in cur_name
              if not ALLERGEN_RE.match(t) and not OR_RE.match(t)
              and t not in NOISE_FB and not INT_PRICE_RE.search(t)
              and not EXT_RE.match(t)]
        while toks and ALLERGEN_RE.match(toks[-1]): toks.pop()
        name=' '.join(toks).strip()
        if cur_cat and name and len(name)>=3 and cur_cat not in seen_cats:
            dishes.append({'kategorie':cur_cat,'name':name,
                           'preis_int':price+'\u00a0\u20ac' if price else '',
                           'vv':cur_vv,'oder':'','oder_preis':''})
            seen_cats.add(cur_cat)
        cur_name=[]; cur_vv=''; after_oder=False; oder_name=[]

    def flush_oder():
        nonlocal after_oder,oder_name
        toks=[t for t in oder_name
              if not ALLERGEN_RE.match(t) and t not in NOISE_FB
              and not INT_PRICE_RE.search(t) and not EXT_RE.match(t)]
        while toks and ALLERGEN_RE.match(toks[-1]): toks.pop()
        name=' '.join(toks).strip()
        if name and len(name)>=3:
            for dish in reversed(dishes):
                if dish['kategorie']==cur_cat:
                    dish['oder']=name; break
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


# ── JS: click day tab ────────────────────────────────────────────────────────────────
JS_CLICK_TAB = r"""
(function(dateStr){
  // Try role=tab first, then any clickable element containing the date string
  var tabs=Array.from(document.querySelectorAll('[role="tab"], .mdc-tab, .mat-tab-label, .mat-mdc-tab'));
  var all=Array.from(document.querySelectorAll('button,a,div,span,li')).filter(el=>{
    var t=(el.innerText||el.textContent||'').trim();
    return t.includes(dateStr) && t.length<40;
  });
  var target=tabs.concat(all).find(el=>(el.innerText||el.textContent||'').trim().includes(dateStr));
  if(target){target.click();return(target.innerText||target.textContent||'').trim().slice(0,60);}
  return null;
})
"""

JS_TAB_TEXT = r"""
(function(){
  var panels=Array.from(document.querySelectorAll('mat-tab-nav-panel,[role="tabpanel"],mat-tab-body'));
  var active=panels.find(p=>{var s=window.getComputedStyle(p);return s.display!=='none'&&s.visibility!=='hidden'&&p.offsetHeight>0;})||panels[0];
  if(!active){var b=document.body.cloneNode(true);b.querySelectorAll('script,style,noscript').forEach(e=>e.remove());return b.innerText||b.textContent||'';}
  return active.innerText||active.textContent||'';
})()
"""

# ── Scrape one day ────────────────────────────────────────────────────────────
def scrape_day(page, date_obj):
    url=f"{URL_BASE}/date/{date_obj.strftime('%Y-%m-%d')}"
    if _SID: url+=f"?ste_sid={_SID}"
    date_label=date_obj.strftime('%d.%m')
    print(f"\n[scrape] {url}  [{date_label}]")

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)

    # Re-inject language so Angular boots in German
    _inject(page)
    page.wait_for_timeout(400)

    dismiss_cookie(page)

    # Wait for category headers (German or English)
    found_sel=None
    for sel in ["h3.category-header","text=Suppe / Vorspeise","text=Suppe",
                "text=Essen 1","text=Soup / Starter","text=Food 1",
                ".category-grid","app-category-list"]:
        try:
            page.wait_for_selector(sel, timeout=12000)
            found_sel=sel; print(f"  [wait] found selector: {sel!r}"); break
        except: pass
    if not found_sel:
        print("  [wait] WARNING: no menu selector found after 12s, waiting extra 5s")
        page.wait_for_timeout(5000)

    # Debug: list all found category headers
    try:
        hdrs=page.evaluate("()=>Array.from(document.querySelectorAll('h3.category-header,h3')).map(h=>h.textContent.trim()).filter(Boolean)")
        print(f"  [debug] h3 headers found: {hdrs}")
    except: pass

    # Click the correct day tab
    before_sig=' '.join((page.evaluate(JS_TAB_TEXT) or '').split()[:8])
    clicked=page.evaluate(f"({JS_CLICK_TAB})('{date_label}')")
    print(f"  [tab] click result: {clicked!r}")

    if clicked:
        for attempt in range(25):
            page.wait_for_timeout(200)
            sig=' '.join((page.evaluate(JS_TAB_TEXT) or '').split()[:8])
            if sig!=before_sig:
                print(f"  [tab] content changed after {attempt+1} polls"); break
        else:
            print("  [tab] content unchanged (already on correct tab)")
    else:
        print(f"  [tab] no tab element found for {date_label!r}, using current content")
        page.wait_for_timeout(800)

    # Debug: category headers after tab switch
    try:
        hdrs2=page.evaluate("()=>Array.from(document.querySelectorAll('h3.category-header')).map(h=>h.textContent.trim())")
        print(f"  [debug] category-header after tab: {hdrs2}")
    except: pass

    # ── PRIMARY: DOM extractor ─────────────────────────────────────────────
    dishes=[]
    try:
        raw_json=page.evaluate(JS_EXTRACT)
        print(f"  [dom] JS_EXTRACT raw length: {len(raw_json) if raw_json else 0}")
        if raw_json and raw_json!='[]':
            dishes=parse_dom_result(raw_json)
            print(f"  [dom] parsed {len(dishes)} dishes via DOM extractor")
        else:
            print("  [dom] JS_EXTRACT returned empty, falling back")
    except Exception as e:
        print(f"  [dom] JS_EXTRACT error: {e}")

    # ── FALLBACK: innerText line parser ─────────────────────────────────────
    if not dishes:
        print("  [fallback] using innerText line parser")
        raw=page.evaluate(JS_TAB_TEXT) or ''
        lines=[l.strip() for l in raw.splitlines() if l.strip()]
        print(f"  [fallback] {len(lines)} lines")
        for i,l in enumerate(lines[:40]):
            print(f"    {i:3d}: {l[:100]}")
        dishes=parse_flat_fallback(lines)
        print(f"  [fallback] parsed {len(dishes)} dishes")

    for dish in dishes:
        print(f"  [result] {dish['kategorie']:8s} | vv={dish['vv']!r:3s} | {dish['name'][:30]!r}"
              +(f" | oder: {dish['oder'][:20]!r}" if dish.get('oder') else ''))
    return dishes


# ── Render ────────────────────────────────────────────────────────────────────
def wrap_text(draw,text,f,max_w,max_lines=4):
    out,cur=[],[]
    for w in text.split():
        t=(' '.join(cur+[w]))
        b=draw.textbbox((0,0),t,font=f)
        if b[2]-b[0]<=max_w: cur.append(w)
        else:
            if cur: out.append(' '.join(cur))
            cur=[w]
        if len(out)>=max_lines-1 and cur: break
    if cur: out.append(' '.join(cur))
    return out[:max_lines]

CATS=['Suppe','Essen 1','Essen 2','Essen 3']
CAT_LABEL={'Suppe':'Suppe','Essen 1':'Essen 1','Essen 2':'Essen 2','Essen 3':'Essen 3'}

def render(week_data,kw,label,local_dt,url_menu,holiday_map,today_date,source=''):
    img=Image.new('RGB',(W,H),(255,255,255))
    d=ImageDraw.Draw(img)
    ftit=lf(20,True); fday=lf(15,True); ftxt=lf(15)
    fsmall=lf(13); fbdg=lf(12,True); fprc=lf(12); fftr=lf(10); fstb=lf(11,True)
    HDR_H=48; DAY_H=28; LEGEND_H=22; STUB_W=56

    d.rectangle([(0,0),(W,HDR_H)],fill=BLUE)
    title=f'Siemens Kantine Regensburg  \u2502  KW {kw:02d}'
    b=d.textbbox((0,0),title,font=ftit)
    d.text(((W-(b[2]-b[0]))//2,(HDR_H-(b[3]-b[1]))//2),title,font=ftit,fill=WHITE)
    y=HDR_H

    all_days=list(holiday_map.keys()); dw=(W-STUB_W)//len(all_days)
    d.rectangle([(0,y),(STUB_W-1,y+DAY_H-1)],fill=BLUE)
    for i,day in enumerate(all_days):
        x=STUB_W+i*dw
        is_hol=holiday_map[day] is not None; is_past=_is_past(day,today_date)
        col=C_HOL_HDR if is_hol else (C_PAST_TXT if is_past else LIGHT)
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)],fill=col)
        b=d.textbbox((0,0),day,font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2,y+(DAY_H-(b[3]-b[1]))//2),day,font=fday,fill=WHITE)
        d.line([(x,y),(x,y+DAY_H)],fill=BLUE,width=1)
    y+=DAY_H

    avail=H-y-FOOTER_H-LEGEND_H-4
    rs=int(avail*0.17); re_=(avail-rs)//3
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
            is_hol=holiday_map[day] is not None; is_past=_is_past(day,today_date)
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
                        d.text((x+(dw-(b2[2]-b2[0]))//2,cy),ln,font=ftxt,fill=C_HOL_TXT);cy+=17
                continue

            PAD=6
            items=[it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items:
                b=d.textbbox((0,0),'–',font=ftxt)
                d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-8),'–',font=ftxt,fill=(180,180,180))
                continue

            it=items[0]; cx=x+PAD; cy=y+PAD; avw=dw-2*PAD
            if it['vv']:
                bl='Vegan' if it['vv']=='VG' else 'Veg.'
                bc=C_VG if it['vv']=='VG' else C_V
                b=d.textbbox((0,0),bl,font=fbdg)
                bw=b[2]-b[0]+7;bh2=b[3]-b[1]+4
                d.rounded_rectangle([(cx,cy),(cx+bw,cy+bh2)],radius=3,fill=bc)
                d.text((cx+4,cy+2),bl,font=fbdg,fill=WHITE);cy+=bh2+4

            oder=it.get('oder','')
            space_oder=(2*16) if oder else 0
            avail_name=rh-(cy-y)-16-space_oder
            max_ln=max(1,min(4,avail_name//17))
            for ln in wrap_text(d,it['name'],ftxt,avw,max_ln):
                d.text((cx,cy),ln,font=ftxt,fill=C_TXT);cy+=17

            if oder:
                for ln in wrap_text(d,f"oder: {oder}",fsmall,avw,2):
                    d.text((cx,cy),ln,font=fsmall,fill=(100,130,160));cy+=16

            if it['preis_int']:
                pl=f"Int: {it['preis_int']}"
                b=d.textbbox((0,0),pl,font=fprc)
                d.text((x+dw-(b[2]-b[0])-PAD,y+rh-(b[3]-b[1])-4),pl,font=fprc,fill=LIGHT)
        y+=rh

    d.line([(0,y),(W,y)],fill=GRID,width=1);y+=1
    d.rectangle([(0,y),(W,y+LEGEND_H)],fill=(245,249,253))
    lx=8
    for col,txt in [(C_VG,'Vegan'),(C_V,'Vegetarisch'),(C_HOL_HDR,'Feiertag'),(C_PAST_BG,'vergangen')]:
        d.rectangle([(lx,y+5),(lx+14,y+15)],fill=col)
        b=d.textbbox((0,0),txt,font=fprc)
        d.text((lx+18,y+4),txt,font=fprc,fill=C_TXT);lx+=18+(b[2]-b[0])+18
    d.text((lx,y+4),'Int = Mitarbeiterpreis',font=fprc,fill=(120,120,120))
    _footer(d,kw,label,local_dt,fftr,source)
    return img

def _is_past(day_key_str,today_date):
    try:
        dm=day_key_str.split(' ')[1].split('.')
        return date(today_date.year,int(dm[1]),int(dm[0]))<today_date
    except: return False

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
    now=datetime.now(timezone.utc); local=german_time(now)
    label,kw=kw_label(now); today_date=local.date()
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
        warmup(page, URL_BASE)
        for date_obj in scrape_dates:
            dk=day_key(date_obj)
            dishes=scrape_day(page,date_obj)
            if dishes: week_data[dk]=dishes
        browser.close()

    days_filled=len(week_data); days_avail=len(scrape_dates)
    print(f'\nErgebnis   : {list(week_data.keys())}  ({days_filled}/{days_avail})')

    img=render(week_data,kw,label,local,URL_BASE,holiday_map,today_date,
               f'DOM ({days_filled}/{days_avail} Tage)')
    img.save(str(out_path),'JPEG',quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')

    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')

if __name__=='__main__':
    main()
