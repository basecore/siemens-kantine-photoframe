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
  3. "oder"-alternative only shown when the alternative name differs from
     the main dish name (case-insensitive).
  4. Fallback chain: DOM-query → innerText line-parser (legacy).
  5. WEEK_OFFSET env var (default 1): 0=current week, 1=next week.
     When offset > 0 all days of that week are scraped (no "past" filter).
  6. Unknown categories (Vegan, Vegetarisch, Fisch, …) map to next free
     Essen slot (2 or 3) for the current day.
  7. "wahlweise dazu"-products: the marker product is skipped, the NEXT
     product is stored as 'zusatz' on the main dish.
  8. wrap_text: respects existing hyphens before splitting characters.
  9. render: uniform font size per row (smallest that fits all cells).
 10. Stub labels drawn last so grid lines don't overwrite them.
"""
import os, re, json
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ── Config ────────────────────────────────────────────────────────────────────
URL_BASE    = os.environ.get("CATERINGPORTAL_URL",
                              "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen")
_SID        = os.environ.get("CATERINGPORTAL_SID", "").strip()
WEEK_OFFSET = int(os.environ.get("WEEK_OFFSET", "1"))   # 0=aktuelle Woche, 1=nächste Woche

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
        except ValueError: start_idx = 1
        for slot in _ESSEN_SLOTS[start_idx:]:
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

# "wahlweise" marker – the product name itself may be just "wahlweise dazu"
# or "wahlweise dazu <ingredient>". We handle both cases:
#  - if after stripping the marker there's still text → that's the zusatz name
#  - if the remaining text is empty or just noise → take the NEXT product as name
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
        skip_next_as_oder=False
        next_is_zusatz=False   # flag: next product is the wahlweise ingredient

        for p in prods:
            name=p['name'].strip().replace('\n',' / ')

            # ── wahlweise handling ──────────────────────────────────────────
            if WAHLWEISE_RE.match(name):
                remainder = WAHLWEISE_RE.sub('', name).strip()
                if remainder and main_dish is not None:
                    # Inline: "wahlweise dazu Parmesan" → remainder = "Parmesan"
                    main_dish['zusatz'] = f"wahlweise: {remainder}"
                    main_dish['zusatz_preis'] = p['intPrice']
                    print(f"    [parse] zusatz (inline): {remainder!r} | Int:{p['intPrice']!r}")
                elif main_dish is not None:
                    # Split: marker product has no ingredient → next product is it
                    next_is_zusatz = True
                    print(f"    [parse] zusatz marker found, next product = ingredient")
                continue

            if next_is_zusatz:
                # This product is the ingredient for wahlweise
                if main_dish is not None:
                    main_dish['zusatz'] = f"wahlweise: {name}"
                    main_dish['zusatz_preis'] = p['intPrice']
                    print(f"    [parse] zusatz (next): {name!r} | Int:{p['intPrice']!r}")
                next_is_zusatz = False
                continue

            # ── oder separator ──────────────────────────────────────────────
            if re.match(r'^(oder|or)$', name.strip(), re.IGNORECASE):
                skip_next_as_oder=True; print(f"    [parse] 'oder' separator"); continue

            # ── main dish or oder alternative ──────────────────────────────
            if main_dish is None:
                vv = vv_from_name(name) or cat_vv
                main_dish={'kategorie':cat,'name':name,'preis_int':p['intPrice'],
                           'vv':vv,'oder':'','oder_preis':'','oder_vv':'',
                           'zusatz':'','zusatz_preis':''}
                skip_next_as_oder=False
                print(f"    [parse] main: {name!r} | Int:{p['intPrice']!r}")
            elif skip_next_as_oder or main_dish['oder']=='':
                if _names_differ(main_dish['name'],name):
                    main_dish['oder']=name
                    main_dish['oder_preis']=p['intPrice']
                    main_dish['oder_vv']=vv_from_name(name)
                    print(f"    [parse] oder: {name!r} vv={main_dish['oder_vv']!r}")
                else:
                    print(f"    [parse] skip dup oder: {name!r}")
                skip_next_as_oder=False

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
            dishes.append({'kategorie':cur_cat,'name':name,'preis_int':price+'\u00a0\u20ac' if price else '','vv':cur_vv,'oder':'','oder_preis':'','oder_vv':'','zusatz':'','zusatz_preis':''})
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
(function(){var panels=Array.from(document.querySelectorAll('mat-tab-nav-panel,[role="tabpanel"],mat-tab-body'));var active=panels.find(p=>{var s=window.getComputedStyle(p);return s.display!=='none'&&s.visibility!=='hidden'&&p.offsetHeight>0;})||panels[0];if(!active){var b=document.body.cloneNode(true);b.querySelectorAll('script,style,noscript').forEach(e=>e.remove());return b.innerText||b.textContent||'';}return active.innerText||active.textContent||'';})()
"""

# ── Scrape one day ────────────────────────────────────────────────────────────
def scrape_day(page, date_obj):
    url=f"{URL_BASE}/date/{date_obj.strftime('%Y-%m-%d')}"
    if _SID: url+=f"?ste_sid={_SID}"
    date_label=date_obj.strftime('%d.%m')
    print(f"\n[scrape] {url}  [{date_label}]")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(800)
    _inject(page)
    page.wait_for_timeout(400)
    dismiss_cookie(page)
    found_sel=None
    for sel in ["h3.category-header","text=Suppe / Vorspeise","text=Suppe","text=Essen 1","text=Soup / Starter","text=Food 1",".category-grid","app-category-list"]:
        try:
            page.wait_for_selector(sel,timeout=12000); found_sel=sel; print(f"  [wait] found: {sel!r}"); break
        except: pass
    if not found_sel:
        print("  [wait] WARNING: no menu selector, extra 5s"); page.wait_for_timeout(5000)
    try:
        hdrs=page.evaluate("()=>Array.from(document.querySelectorAll('h3.category-header,h3')).map(h=>h.textContent.trim()).filter(Boolean)")
        print(f"  [debug] h3 headers: {hdrs}")
    except: pass
    before_sig=' '.join((page.evaluate(JS_TAB_TEXT) or '').split()[:8])
    clicked=page.evaluate(f"({JS_CLICK_TAB})('{date_label}')")
    print(f"  [tab] click result: {clicked!r}")
    if clicked:
        for attempt in range(25):
            page.wait_for_timeout(200)
            if ' '.join((page.evaluate(JS_TAB_TEXT) or '').split()[:8])!=before_sig:
                print(f"  [tab] content changed after {attempt+1} polls"); break
        else: print("  [tab] content unchanged")
    else:
        print(f"  [tab] no tab for {date_label!r}"); page.wait_for_timeout(800)
    try:
        hdrs2=page.evaluate("()=>Array.from(document.querySelectorAll('h3.category-header')).map(h=>h.textContent.trim())")
        print(f"  [debug] category-header after tab: {hdrs2}")
    except: pass
    dishes=[]
    try:
        raw_json=page.evaluate(JS_EXTRACT)
        print(f"  [dom] JS_EXTRACT length: {len(raw_json) if raw_json else 0}")
        if raw_json and raw_json!='[]':
            dishes=parse_dom_result(raw_json); print(f"  [dom] {len(dishes)} dishes")
        else: print("  [dom] empty, falling back")
    except Exception as e: print(f"  [dom] error: {e}")
    if not dishes:
        raw=page.evaluate(JS_TAB_TEXT) or ''
        lines=[l.strip() for l in raw.splitlines() if l.strip()]
        print(f"  [fallback] {len(lines)} lines")
        for i,l in enumerate(lines[:40]): print(f"    {i:3d}: {l[:100]}")
        dishes=parse_flat_fallback(lines)
    for dish in dishes:
        zusatz_str = f" | zusatz: {dish.get('zusatz','')[:40]!r}" if dish.get('zusatz') else ''
        print(f"  [result] {dish['kategorie']:8s} | vv={dish['vv']!r:3s} | {dish['name'][:35]!r}"
              +(f" | oder: {dish['oder'][:25]!r} vv={dish.get('oder_vv','')!r}" if dish.get('oder') else '')
              +zusatz_str)
    return dishes


# ── Render helpers ─────────────────────────────────────────────────────────────

def _split_long_word(draw, word, font, max_w):
    """Splits a word that exceeds max_w, preferring existing hyphens."""
    if '-' in word:
        parts = word.split('-')
        chunks = []
        cur = ''
        for i, part in enumerate(parts):
            candidate = (cur + '-' + part) if cur else part
            test = candidate + ('-' if i < len(parts)-1 else '')
            b = draw.textbbox((0,0), test, font=font)
            if b[2]-b[0] <= max_w:
                cur = candidate
            else:
                if cur: chunks.append(cur + '-')
                cur = part
        if cur: chunks.append(cur)
        result = []
        for chunk in chunks:
            b = draw.textbbox((0,0), chunk.rstrip('-'), font=font)
            if b[2]-b[0] > max_w:
                result.extend(_split_chars(draw, chunk.rstrip('-'), font, max_w))
                if chunk.endswith('-') and result:
                    result[-1] = result[-1].rstrip('-') + '-'
            else:
                result.append(chunk)
        return result
    return _split_chars(draw, word, font, max_w)


def _split_chars(draw, word, font, max_w):
    parts = []
    while word:
        lo, hi = 1, len(word)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            test = word[:mid] + ('-' if mid < len(word) else '')
            b = draw.textbbox((0,0), test, font=font)
            if b[2]-b[0] <= max_w: lo = mid
            else: hi = mid - 1
        if lo <= 0: lo = 1
        chunk = word[:lo]; word = word[lo:]
        parts.append(chunk + ('-' if word else ''))
    return parts


def wrap_text(draw, text, f, max_w, max_lines=20):
    tokens = []
    for word in text.split():
        b = draw.textbbox((0,0), word, font=f)
        if b[2]-b[0] > max_w:
            tokens.extend(_split_long_word(draw, word, f, max_w))
        else:
            tokens.append(word)
    out, cur = [], []
    for tok in tokens:
        t = ' '.join(cur + [tok])
        b = draw.textbbox((0,0), t, font=f)
        if b[2]-b[0] <= max_w:
            cur.append(tok)
        else:
            if cur: out.append(' '.join(cur))
            cur = [tok]
    if cur: out.append(' '.join(cur))
    return out[:max_lines]


def _find_uniform_font_size(draw, texts, max_w, max_h, size_start=19, size_min=10, bold=False):
    """Find the largest font size where ALL texts fit within max_w x max_h."""
    for size in range(size_start, size_min - 1, -1):
        f = lf(size, bold)
        all_fit = True
        for text in texts:
            if not text: continue
            lines = wrap_text(draw, text, f, max_w, max_lines=20)
            if not lines: continue
            b = draw.textbbox((0,0), lines[0], font=f)
            lh = b[3]-b[1]+2
            if lh * len(lines) > max_h:
                all_fit = False
                break
        if all_fit:
            return size
    return size_min


def _fit_font(draw, text, max_w, max_h, size_start=19, size_min=10, bold=False):
    """Returns (font, line_height, wrapped_lines) that fit in max_w x max_h."""
    for size in range(size_start, size_min - 1, -1):
        f = lf(size, bold)
        lines = wrap_text(draw, text, f, max_w, max_lines=20)
        if not lines: return f, size+2, lines
        b = draw.textbbox((0,0), lines[0], font=f)
        lh = b[3]-b[1]+2
        if lh * len(lines) <= max_h:
            return f, lh, lines
    f = lf(size_min, bold)
    lines = wrap_text(draw, text, f, max_w, max_lines=20)
    b = draw.textbbox((0,0), lines[0], font=f) if lines else (0,0,0,size_min)
    return f, b[3]-b[1]+2, lines


def _is_past(day_key_str, today_date):
    try:
        dm=day_key_str.split(' ')[1].split('.')
        return date(today_date.year,int(dm[1]),int(dm[0]))<today_date
    except: return False

def _is_today(day_key_str, today_date):
    try:
        dm=day_key_str.split(' ')[1].split('.')
        return date(today_date.year,int(dm[1]),int(dm[0]))==today_date
    except: return False

CATS=['Suppe','Essen 1','Essen 2','Essen 3']
CAT_LABEL={'Suppe':'Su.','Essen 1':'E1','Essen 2':'E2','Essen 3':'E3'}


def render(week_data, kw, label, local_dt, url_menu, holiday_map, today_date,
           monday_date, source=''):
    img=Image.new('RGB',(W,H),(255,255,255))
    d=ImageDraw.Draw(img)

    ftit  = lf(22, True)
    fdate = lf(13)
    fday  = lf(19, True)
    fbdg  = lf(13, True)
    fprc  = lf(14, True)
    fftr  = lf(11)
    fstb  = lf(11, True)
    fleg  = lf(12)

    HDR_H   = 52
    DAY_H   = 34
    LEGEND_H= 22
    STUB_W  = 38
    TODAY_BW= 3
    PAD     = 5

    # ── header ────────────────────────────────────────────────────────────────
    d.rectangle([(0,0),(W,HDR_H)],fill=BLUE)
    friday_date = monday_date + timedelta(4)
    date_range  = f"{monday_date.strftime('%d.%m.%Y')} – {friday_date.strftime('%d.%m.%Y')}"
    title_str   = f"Siemens Kantine Regensburg  |  KW {kw:02d}"
    bt=d.textbbox((0,0),title_str,font=ftit)
    title_h=bt[3]-bt[1]
    bd=d.textbbox((0,0),date_range,font=fdate)
    date_h=bd[3]-bd[1]
    total_h=title_h+3+date_h
    ty=(HDR_H-total_h)//2
    d.text(((W-(bt[2]-bt[0]))//2, ty), title_str, font=ftit, fill=WHITE)
    d.text(((W-(bd[2]-bd[0]))//2, ty+title_h+3), date_range, font=fdate, fill=(180,210,240))
    y=HDR_H

    # ── day headers ───────────────────────────────────────────────────────────
    all_days=list(holiday_map.keys()); dw=(W-STUB_W)//len(all_days)
    d.rectangle([(0,y),(STUB_W-1,y+DAY_H-1)],fill=BLUE)
    for i,day in enumerate(all_days):
        x=STUB_W+i*dw
        is_hol  = holiday_map[day] is not None
        is_past = _is_past(day, today_date)
        is_today= _is_today(day, today_date)
        if is_today:    col = C_TODAY_HDR
        elif is_hol:    col = C_HOL_HDR
        elif is_past:   col = C_PAST_TXT
        else:           col = LIGHT
        d.rectangle([(x,y),(x+dw-1,y+DAY_H-1)],fill=col)
        b=d.textbbox((0,0),day,font=fday)
        d.text((x+(dw-(b[2]-b[0]))//2, y+(DAY_H-(b[3]-b[1]))//2), day, font=fday, fill=WHITE)
        d.line([(x,y),(x,y+DAY_H)],fill=BLUE,width=1)
        if is_today:
            d.line([(x,y),(x+dw,y)],fill=C_TODAY,width=TODAY_BW)
    y+=DAY_H

    avail=H-y-FOOTER_H-LEGEND_H-4
    rs=int(avail*0.17)
    re_=(avail-rs)//3
    ROW_H={'Suppe':rs,'Essen 1':re_,'Essen 2':re_,'Essen 3':avail-rs-2*re_}

    # ── rows ──────────────────────────────────────────────────────────────────
    for ri,cat in enumerate(CATS):
        rh=ROW_H[cat]
        d.line([(0,y),(W,y)],fill=GRID,width=1)

        avw = dw - 2*PAD

        # ── Pre-pass: compute uniform font size for this row ──────────────
        # Collect all main dish name texts for non-past, non-holiday cells
        f_sm_size = 11
        f_sm = lf(f_sm_size)
        b_sm = d.textbbox((0,0),'x',font=f_sm)
        lh_sm = b_sm[3]-b_sm[1]+2

        # For each day, estimate available height for the name
        # (after badge, reserving space for oder + zusatz + price)
        candidate_texts = []
        for day in all_days:
            if holiday_map[day] is not None: continue
            if _is_past(day, today_date): continue
            items=[it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items: continue
            it = items[0]
            badge_h = 0
            if it['vv']:
                b=d.textbbox((0,0),'Vegan',font=fbdg)
                badge_h = b[3]-b[1]+4+3
            pr_h = 0
            if it['preis_int']:
                pb = d.textbbox((0,0),f"Int: {it['preis_int']}",font=fprc)
                pr_h = pb[3]-pb[1]+2
            extra = (1 if it.get('oder') else 0) + (1 if it.get('zusatz') else 0)
            reserved = lh_sm * extra + pr_h + 4
            avail_name = rh - PAD - badge_h - reserved
            if avail_name < 12: avail_name = 12
            candidate_texts.append((it['name'], avail_name))

        # Find the single font size that works for ALL cells in this row
        uniform_size = 19
        if candidate_texts:
            # Use the most constrained cell (smallest avail_name) as reference
            min_avail = min(a for _, a in candidate_texts)
            all_names = [t for t, _ in candidate_texts]
            uniform_size = _find_uniform_font_size(d, all_names, avw, min_avail,
                                                    size_start=19, size_min=10)

        fn_uniform = lf(uniform_size)
        b_un = d.textbbox((0,0),'x',font=fn_uniform)
        lhn_uniform = b_un[3]-b_un[1]+2

        # ── Draw each day cell ─────────────────────────────────────────────
        for i,day in enumerate(all_days):
            x=STUB_W+i*dw
            is_hol  = holiday_map[day] is not None
            is_past = _is_past(day, today_date)
            is_today= _is_today(day, today_date)

            if is_past:     bg = C_PAST_BG
            elif is_hol:    bg = C_HOL_BG
            elif is_today:  bg = (255, 253, 230)
            else:           bg = R_ODD if ri%2==0 else R_EVEN

            d.rectangle([(x,y),(x+dw-1,y+rh-1)],fill=bg)
            d.line([(x,y),(x,y+rh)],fill=GRID,width=1)
            if is_today:
                d.line([(x,y),(x,y+rh)],fill=C_TODAY,width=TODAY_BW)
                d.line([(x+dw-1,y),(x+dw-1,y+rh)],fill=C_TODAY,width=TODAY_BW)

            if is_past:
                if ri==0:
                    b=d.textbbox((0,0),'vergangen',font=fprc)
                    d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-10),'vergangen',font=fprc,fill=C_PAST_TXT)
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
                    fh,lhh,hlines=_fit_font(d,hn,dw-8,rh-cy+y-4)
                    for ln in hlines:
                        b2=d.textbbox((0,0),ln,font=fh)
                        d.text((x+(dw-(b2[2]-b2[0]))//2,cy),ln,font=fh,fill=C_HOL_TXT);cy+=lhh
                continue

            items=[it for it in week_data.get(day,[]) if it['kategorie']==cat]
            if not items:
                b=d.textbbox((0,0),'–',font=lf(19))
                d.text((x+(dw-(b[2]-b[0]))//2,y+rh//2-11),'–',font=lf(19),fill=(180,180,180))
                continue

            it=items[0]
            cy=y+PAD

            # Badge
            if it['vv']:
                bl='Vegan' if it['vv']=='VG' else 'Veg.'
                bc=C_VG if it['vv']=='VG' else C_V
                b=d.textbbox((0,0),bl,font=fbdg)
                bw2=b[2]-b[0]+8;bh2=b[3]-b[1]+4
                d.rounded_rectangle([(x+PAD,cy),(x+PAD+bw2,cy+bh2)],radius=3,fill=bc)
                d.text((x+PAD+4,cy+2),bl,font=fbdg,fill=WHITE);cy+=bh2+3

            oder      = it.get('oder','')
            oder_vv   = it.get('oder_vv','')
            zusatz    = it.get('zusatz','')
            zusatz_pr = it.get('zusatz_preis','')

            # Main dish name with uniform font
            name_lines = wrap_text(d, it['name'], fn_uniform, avw, max_lines=20)
            for ln in name_lines:
                d.text((x+PAD, cy), ln, font=fn_uniform, fill=C_TXT); cy += lhn_uniform

            # oder line
            if oder:
                ocx = x+PAD
                if oder_vv:
                    obl='Vegan' if oder_vv=='VG' else 'Veg.'
                    obc=C_VG if oder_vv=='VG' else C_V
                    ob=d.textbbox((0,0),obl,font=fbdg)
                    obw=ob[2]-ob[0]+6; obh=ob[3]-ob[1]+4
                    d.rounded_rectangle([(ocx,cy),(ocx+obw,cy+obh)],radius=3,fill=obc)
                    d.text((ocx+3,cy+2),obl,font=fbdg,fill=WHITE)
                    ocx += obw+3
                avail_oder = x+dw-PAD-ocx
                # Use same uniform size for oder too, capped at 13
                oder_size = min(uniform_size, 13)
                fo,lho,oder_lines=_fit_font(d,f"oder: {oder}",avail_oder,lh_sm*3,
                                             size_start=oder_size,size_min=10)
                for ln in oder_lines:
                    d.text((ocx,cy),ln,font=fo,fill=(80,120,180));cy+=lho

            # wahlweise-Zusatz line
            if zusatz:
                ztext = zusatz
                if zusatz_pr:
                    ztext += f"  Int: {zusatz_pr}"
                fz,lhz,z_lines=_fit_font(d,ztext,avw,lh_sm*3,
                                          size_start=min(uniform_size,13),size_min=10)
                for ln in z_lines:
                    d.text((x+PAD,cy),ln,font=fz,fill=C_ZUSATZ);cy+=lhz

            # Price bottom-right
            if it['preis_int']:
                pl=f"Int: {it['preis_int']}"
                b=d.textbbox((0,0),pl,font=fprc)
                d.text((x+dw-(b[2]-b[0])-PAD, y+rh-(b[3]-b[1])-3), pl, font=fprc, fill=LIGHT)

            if is_today:
                d.line([(x,y+rh-1),(x+dw,y+rh-1)],fill=C_TODAY,width=TODAY_BW)

        # ── Draw stub label AFTER all cells (so it's never overwritten) ───
        # Fill the stub background first
        d.rectangle([(0,y),(STUB_W-1,y+rh-1)],fill=BLUE)
        # Re-draw left grid border
        d.line([(STUB_W,y),(STUB_W,y+rh)],fill=GRID,width=1)
        # Render label text rotated 90°
        lbl=CAT_LABEL[cat]
        b=d.textbbox((0,0),lbl,font=fstb)
        tmp=Image.new('RGBA',(b[3]-b[1]+4,b[2]-b[0]+4),(0,0,0,0))
        td=ImageDraw.Draw(tmp); td.text((2,2),lbl,font=fstb,fill=WHITE)
        tmp_r=tmp.rotate(90,expand=True)
        px=max(0,(STUB_W-tmp_r.width)//2)
        py=y+(rh-tmp_r.height)//2
        img.paste(tmp_r,(px,py),tmp_r)

        y+=rh

    # today bottom border
    for i,day in enumerate(all_days):
        if _is_today(day,today_date):
            x=STUB_W+i*dw
            d.line([(x,y),(x+dw,y)],fill=C_TODAY,width=TODAY_BW)

    # ── legend ────────────────────────────────────────────────────────────────
    d.line([(0,y),(W,y)],fill=GRID,width=1);y+=1
    d.rectangle([(0,y),(W,y+LEGEND_H)],fill=(245,249,253))
    lx=6
    for col,txt in [(C_VG,'Vegan'),(C_V,'Vegetarisch'),(C_HOL_HDR,'Feiertag'),
                    (C_PAST_BG,'vergangen'),(C_TODAY,'Heute')]:
        d.rectangle([(lx,y+5),(lx+12,y+15)],fill=col)
        b=d.textbbox((0,0),txt,font=fleg)
        d.text((lx+15,y+4),txt,font=fleg,fill=C_TXT);lx+=15+(b[2]-b[0])+12
    d.text((lx,y+4),'Int = Mitarbeiterpreis',font=fleg,fill=(120,120,120))

    _footer(d,kw,label,local_dt,fftr,source)
    return img


def _footer(d,kw,label,local_dt,f,source=''):
    src=f' – {source}' if source else ''
    txt=(f'KW {kw:02d} / {label}  –  '
         f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  –  "
         f"siemens.cateringportal.io{src}")
    d.rectangle([(0,H-FOOTER_H),(W,H)],fill=BLUE)
    b=d.textbbox((0,0),txt,font=f)
    d.text(((W-(b[2]-b[0]))//2, H-FOOTER_H+(FOOTER_H-(b[3]-b[1]))//2), txt, font=f, fill=WHITE)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now=datetime.now(timezone.utc); local=german_time(now)
    today_date=local.date()
    target_monday=today_date - timedelta(days=today_date.weekday()) + timedelta(weeks=WEEK_OFFSET)
    label,kw=kw_label(datetime.combine(target_monday,datetime.min.time(),tzinfo=timezone.utc))
    out_path=OUT_DIR/f'kantine_{label}.jpg'
    print(f'Week label : {label}  (KW {kw:02d})')
    print(f'Today      : {today_date}')
    print(f'WEEK_OFFSET: {WEEK_OFFSET}  →  scraping week of {target_monday}')
    holiday_map=week_holiday_map(target_monday)
    hol_days=[k for k,v in holiday_map.items() if v]
    all_week_dates=[datetime.combine(target_monday+timedelta(i),datetime.min.time(),tzinfo=timezone.utc) for i in range(5)]
    if WEEK_OFFSET==0:
        scrape_dates=[dt for dt in all_week_dates if dt.date()>=today_date and day_key(dt) not in hol_days]
    else:
        scrape_dates=[dt for dt in all_week_dates if day_key(dt) not in hol_days]
    print(f'Feiertage  : {[(k,holiday_map[k]) for k in hol_days] or "keine"}')
    print(f'Scraping   : {[day_key(dt) for dt in scrape_dates]}')
    week_data={}
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        page=browser.new_page(
            viewport={"width":1400,"height":900},
            extra_http_headers={"Accept-Language":"de-DE,de;q=0.9,en;q=0.1"},
        )
        warmup(page,URL_BASE)
        for date_obj in scrape_dates:
            dk=day_key(date_obj)
            dishes=scrape_day(page,date_obj)
            if dishes: week_data[dk]=dishes
        browser.close()
    days_filled=len(week_data); days_avail=len(scrape_dates)
    print(f'\nErgebnis   : {list(week_data.keys())}  ({days_filled}/{days_avail})')
    img=render(week_data,kw,label,local,URL_BASE,holiday_map,today_date,
               target_monday, f'DOM ({days_filled}/{days_avail} Tage)')
    img.save(str(out_path),'JPEG',quality=92)
    print(f'Saved: {out_path}  ({img.size[0]}x{img.size[1]})')
    for old in sorted(OUT_DIR.glob('kantine_*.jpg'))[:-MAX_KEEP]:
        old.unlink(); print(f'Removed: {old}')

if __name__=='__main__':
    main()
