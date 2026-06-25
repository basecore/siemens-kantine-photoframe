#!/usr/bin/env python3
"""Siemens Kantine Regensburg – weekly menu table renderer.

Strategy:
  1. Open cateringportal.io with Playwright (headless Chromium)
  2. Parse menu data from the DOM (no screenshot of page)
  3. Render a clean 600x800 table image with Pillow
  4. Save as JPEG for Philips 8FF3WMI PhotoFrame

Output: docs/images/kantine_YYYY-Www.jpg  (600 x 800 px, portrait)
"""
import os
import json
import textwrap
from pathlib import Path
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

# ── Config ────────────────────────────────────────────────────────────────────
URL_MENU = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen",
)
_SID = os.environ.get("CATERINGPORTAL_SID", "").strip()
if _SID:
    URL_MENU = f"{URL_MENU}?ste_sid={_SID}"

OUT_DIR  = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP = 8

# Canvas: portrait 600x800 (PhotoFrame zeigt auch Portrait)
W, H = 600, 800

# ── Colours ───────────────────────────────────────────────────────────────────
SIEMENS_BLUE  = (0,  57, 107)
SIEMENS_LIGHT = (0, 119, 193)
ROW_ODD       = (240, 246, 252)
ROW_EVEN      = (255, 255, 255)
VG_COLOR      = ( 34, 139,  34)
V_COLOR       = (100, 180,  60)
TEXT_DARK     = ( 30,  30,  30)
TEXT_WHITE    = (255, 255, 255)
GRID_COLOR    = (200, 215, 230)

# ── Font helper ───────────────────────────────────────────────────────────────
_FONT_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]
_FONT_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]

def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    paths = (_FONT_BOLD if bold else _FONT_REGULAR) + _FONT_REGULAR
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()

# ── Time helpers ──────────────────────────────────────────────────────────────
def get_german_time(utc_dt: datetime) -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return utc_dt.astimezone(ZoneInfo("Europe/Berlin"))
    except ImportError:
        pass
    try:
        import pytz
        return utc_dt.astimezone(pytz.timezone("Europe/Berlin"))
    except ImportError:
        pass
    import calendar
    year = utc_dt.year
    def last_sunday(y, m):
        ld = calendar.monthrange(y, m)[1]
        d  = datetime(y, m, ld, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    cest_start = last_sunday(year, 3).replace(hour=1)
    cest_end   = last_sunday(year, 10).replace(hour=1)
    offset = timedelta(hours=2 if cest_start <= utc_dt < cest_end else 1)
    return utc_dt + offset

def get_week_label(utc_dt: datetime) -> str:
    local = get_german_time(utc_dt)
    iso_year, iso_week, _ = local.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"

# ── Playwright navigation ─────────────────────────────────────────────────────
def goto_with_fallback(page, url: str):
    for strategy in ("load", "domcontentloaded", "commit"):
        try:
            print(f"  goto wait_until='{strategy}'...")
            page.goto(url, wait_until=strategy, timeout=60000)
            print(f"  OK: '{strategy}'")
            return
        except Exception as e:
            print(f"  '{strategy}' failed: {e}")
    raise RuntimeError("All navigation strategies failed")

def dismiss_consent(page):
    """Try to dismiss GDPR banner silently."""
    # 1) iframe approach
    for iframe_sel in (
        "iframe[src*='privacy-mgmt.com']",
        "iframe[id*='sp_message']",
        "iframe[name*='sp_message']",
    ):
        try:
            frame = page.frame_locator(iframe_sel)
            btn = frame.locator(
                "button.sp_choice_type_11, "
                "button[title='Akzeptieren und weiter'], "
                "button[aria-label='Akzeptieren und weiter']"
            )
            if btn.count() > 0:
                btn.first.click(timeout=5000)
                page.wait_for_timeout(1500)
                print("  Consent dismissed (iframe).")
                return
        except Exception:
            pass
    # 2) JS removal
    try:
        page.evaluate("""
            () => [
                '#sp_message_container','[id^=sp_message_container]',
                '.sp-message-container','#notice','.message.type-modal'
            ].forEach(s => document.querySelectorAll(s)
                .forEach(e => e.style.display='none'))
        """)
    except Exception:
        pass
    # 3) Direct button
    for sel in (
        'button.sp_choice_type_11',
        'button[title="Akzeptieren und weiter"]',
        'button#acceptAllButton',
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=4000)
                page.wait_for_timeout(1000)
                print(f"  Consent dismissed ({sel}).")
                return
        except Exception:
            pass

# ── DOM Parser ────────────────────────────────────────────────────────────────
DOM_EXTRACT_JS = """
() => {
    const days = [];

    // ----- Strategy A: data-* attributes or JSON in script tags -----
    const scripts = document.querySelectorAll('script[type="application/json"], script[data-drupal-selector]');
    // (reserved for future use)

    // ----- Strategy B: structured card/tile layout ------------------
    // Many catering portals use a repeating .day or [data-day] block
    const dayBlocks = document.querySelectorAll(
        '[data-weekday], .day-column, .menu-day, .weekday-col, "
        + ".tag-spalte, [class*=\"day-\"], [class*=\"weekday\"]'
    );

    if (dayBlocks.length >= 4) {
        dayBlocks.forEach(block => {
            const dayName = (block.querySelector(
                '[class*=\"day-name\"], [class*=\"weekday-name\"], .day-header, h3, h4'
            ) || block).innerText.trim().split('\\n')[0];

            const meals = [];
            block.querySelectorAll(
                '[class*=\"meal\"], [class*=\"dish\"], [class*=\"menu-item\"], [class*=\"speise\"], li.item'
            ).forEach(item => {
                const nameEl = item.querySelector(
                    '[class*=\"name\"], [class*=\"title\"], [class*=\"description\"], p, span'
                );
                const name = nameEl ? nameEl.innerText.trim() : item.innerText.trim().split('\\n')[0];
                const priceEl = item.querySelector('[class*=\"price\"], [class*=\"preis\"]');
                const price   = priceEl ? priceEl.innerText.trim() : '';
                const isVegan = item.classList.toString().toLowerCase().includes('vegan') ||
                                item.querySelector('[alt*=\"vegan\"], [title*=\"vegan\"], [class*=\"vegan\"]') !== null;
                const isVeg   = item.classList.toString().toLowerCase().includes('vegetar') ||
                                item.querySelector('[alt*=\"vegetar\"], [title*=\"vegetar\"], [class*=\"vegetar\"]') !== null;
                if (name.length > 2) {
                    meals.push({ name, price, isVegan, isVeg });
                }
            });
            if (dayName && meals.length > 0) days.push({ day: dayName, meals });
        });
    }

    // ----- Strategy C: table rows -----------------------------------
    if (days.length === 0) {
        document.querySelectorAll('table').forEach(tbl => {
            const rows = tbl.querySelectorAll('tr');
            rows.forEach(row => {
                const cells = row.querySelectorAll('td, th');
                if (cells.length >= 2) {
                    const dayCell  = cells[0].innerText.trim();
                    const mealCell = cells[1].innerText.trim();
                    if (dayCell && mealCell) {
                        days.push({ day: dayCell, meals: [{ name: mealCell, price: '', isVegan: false, isVeg: false }] });
                    }
                }
            });
        });
    }

    // ----- Strategy D: full-page text dump (last resort) ------------
    if (days.length === 0) {
        return { strategy: 'raw_text', text: document.body.innerText.substring(0, 8000), days: [] };
    }

    return { strategy: 'structured', days };
}
"""

DAY_DE = {"mo": "Mo", "di": "Di", "mi": "Mi", "do": "Do", "fr": "Fr",
           "mon": "Mo", "tue": "Di", "wed": "Mi", "thu": "Do", "fri": "Fr",
           "montag": "Mo", "dienstag": "Di", "mittwoch": "Mi",
           "donnerstag": "Do", "freitag": "Fr"}

CATS = ["Suppe", "Essen 1", "Essen 2", "Essen 3"]

def classify_meal(idx: int, name: str) -> str:
    """Guess category from position index and name."""
    nl = name.lower()
    if any(k in nl for k in ("suppe", "soup", "consomm", "brühe", "eintopf")):
        return "Suppe"
    if idx == 0:
        return "Essen 1"
    if idx == 1:
        return "Essen 2"
    return "Essen 3"

def parse_dom_data(raw: dict, local_dt: datetime) -> dict:
    """Convert raw DOM extraction to week_data dict keyed by 'Mo DD.MM' etc."""
    week_data = {}

    if raw.get("strategy") == "raw_text" or not raw.get("days"):
        print("  DOM parse: no structured data found, using raw text fallback.")
        print(raw.get("text", "")[:1000])
        return {}

    # Build Mon–Fri date map for current week
    day_num = local_dt.weekday()  # 0=Mon
    monday  = local_dt - timedelta(days=day_num)
    date_map = {}
    day_names_short = ["Mo", "Di", "Mi", "Do", "Fr"]
    for i, dn in enumerate(day_names_short):
        d = monday + timedelta(days=i)
        date_map[dn] = d.strftime("%d.%m")

    for day_block in raw["days"]:
        raw_day = day_block["day"].strip()
        # Map to Mo/Di/Mi/Do/Fr
        key = raw_day[:2].lower()
        short = DAY_DE.get(key) or DAY_DE.get(raw_day.lower().split()[0], None)
        if not short:
            continue
        label = f"{short} {date_map.get(short, '')}"
        meals = []
        for idx, m in enumerate(day_block["meals"][:4]):
            vv = ""
            if m.get("isVegan"):  vv = "VG"
            elif m.get("isVeg"): vv = "V"
            meals.append({
                "kategorie": classify_meal(idx, m["name"]),
                "name":      m["name"],
                "vv":        vv,
                "preis_int": m.get("price", ""),
            })
        if meals:
            week_data[label] = meals

    return week_data

# ── Image renderer ────────────────────────────────────────────────────────────
def wrap_text(draw, text: str, font, max_width: int) -> list:
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        bb = draw.textbbox((0, 0), test, font=font)
        if bb[2] - bb[0] <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines

def render_table(week_data: dict, kw: int, week_label: str, utc_dt: datetime) -> Image.Image:
    img  = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_title  = load_font(13, bold=True)
    f_day    = load_font(10, bold=True)
    f_cat    = load_font( 8, bold=True)
    f_dish   = load_font( 9, bold=False)
    f_badge  = load_font( 8, bold=True)
    f_price  = load_font( 8, bold=False)
    f_footer = load_font( 9, bold=False)
    f_empty  = load_font(10, bold=False)

    # ── HEADER ────────────────────────────────────────────────────────
    HDR_H = 36
    draw.rectangle([(0, 0), (W, HDR_H)], fill=SIEMENS_BLUE)
    title = f"Siemens Kantine Regensburg  |  KW {kw:02d}"
    bb = draw.textbbox((0, 0), title, font=f_title)
    draw.text(((W - (bb[2]-bb[0])) // 2, 10), title, font=f_title, fill=TEXT_WHITE)
    y = HDR_H

    if not week_data:
        draw.text((20, y + 40), "Kein Speiseplan verfügbar.", font=f_empty, fill=TEXT_DARK)
        draw.text((20, y + 60), "Bitte Seite manuell prüfen:", font=f_empty, fill=TEXT_DARK)
        draw.text((20, y + 80), URL_MENU, font=f_empty, fill=SIEMENS_LIGHT)
        _add_footer(img, draw, kw, week_label, utc_dt, f_footer)
        return img

    days  = list(week_data.keys())
    n     = len(days)
    day_w = W // n

    # ── DAY HEADERS ───────────────────────────────────────────────────
    DAY_H = 22
    for i, day in enumerate(days):
        x0 = i * day_w
        draw.rectangle([(x0, y), (x0 + day_w - 1, y + DAY_H - 1)], fill=SIEMENS_LIGHT)
        bb = draw.textbbox((0, 0), day, font=f_day)
        draw.text((x0 + (day_w - (bb[2]-bb[0])) // 2, y + 5), day, font=f_day, fill=TEXT_WHITE)
        if i > 0:
            draw.line([(x0, y), (x0, y + DAY_H)], fill=SIEMENS_BLUE, width=1)
    y += DAY_H

    # Row heights: Suppe=44, Essen=62 each
    ROW_H = {"Suppe": 44, "Essen 1": 62, "Essen 2": 62, "Essen 3": 62}

    for ci, cat in enumerate(CATS):
        rh = ROW_H[cat]
        bg = ROW_ODD if ci % 2 == 0 else ROW_EVEN
        draw.rectangle([(0, y), (W, y + rh - 1)], fill=bg)
        draw.line([(0, y), (W, y)], fill=GRID_COLOR, width=1)

        for i, day in enumerate(days):
            x0 = i * day_w
            if i > 0:
                draw.line([(x0, y), (x0, y + rh)], fill=GRID_COLOR, width=1)

            dishes = [d for d in week_data.get(day, []) if d["kategorie"] == cat]
            if not dishes:
                # grey dash
                bb = draw.textbbox((0,0), "–", font=f_empty)
                draw.text((x0 + (day_w-(bb[2]-bb[0]))//2, y + rh//2 - 6), "–",
                          font=f_empty, fill=(180,180,180))
                continue

            dish = dishes[0]
            PAD = 4
            cx, cy = x0 + PAD, y + PAD
            avail_w = day_w - PAD * 2

            # Category label
            draw.text((cx, cy), dish["kategorie"], font=f_cat, fill=(110, 110, 110))
            cy += 11

            # Vegan/Veg badge
            if dish["vv"]:
                bc = VG_COLOR if dish["vv"] == "VG" else V_COLOR
                bl = "Vegan" if dish["vv"] == "VG" else "Veg."
                bb = draw.textbbox((0, 0), bl, font=f_badge)
                bw, bh = bb[2]-bb[0]+6, bb[3]-bb[1]+3
                draw.rounded_rectangle([(cx, cy), (cx+bw, cy+bh)], radius=3, fill=bc)
                draw.text((cx+3, cy+1), bl, font=f_badge, fill=TEXT_WHITE)
                cy += bh + 2

            # Dish name (wrapped)
            max_lines = 2 if cat == "Suppe" else 3
            for ln in wrap_text(draw, dish["name"], f_dish, avail_w)[:max_lines]:
                draw.text((cx, cy), ln, font=f_dish, fill=TEXT_DARK)
                cy += 12

            # Price bottom-right
            if dish["preis_int"]:
                pl = f"Int: {dish['preis_int']}"
                bb = draw.textbbox((0, 0), pl, font=f_price)
                draw.text(
                    (x0 + day_w - (bb[2]-bb[0]) - PAD, y + rh - (bb[3]-bb[1]) - PAD - 1),
                    pl, font=f_price, fill=SIEMENS_LIGHT
                )
        y += rh

    # Bottom grid line
    draw.line([(0, y), (W, y)], fill=GRID_COLOR, width=1)
    y += 1

    # ── LEGEND ────────────────────────────────────────────────────────
    LEG_H = 18
    if y + LEG_H + 24 <= H:
        draw.rectangle([(0, y), (W, y + LEG_H)], fill=(245, 249, 253))
        draw.rectangle([( 6, y+4), (18, y+14)], fill=VG_COLOR)
        draw.text((22, y+4), "Vegan",      font=f_price, fill=TEXT_DARK)
        draw.rectangle([(66, y+4), (78, y+14)], fill=V_COLOR)
        draw.text((82, y+4), "Vegetarisch", font=f_price, fill=TEXT_DARK)
        draw.text((170, y+4), "Int = Mitarbeiterpreis", font=f_price, fill=(120,120,120))
        y += LEG_H

    _add_footer(img, draw, kw, week_label, utc_dt, f_footer)
    return img

def _add_footer(img, draw, kw, week_label, utc_dt, font):
    local_dt = get_german_time(utc_dt)
    txt = (f"KW {kw:02d} / {week_label}  –  "
           f"{local_dt.strftime('%d.%m.%Y %H:%M Uhr')}  –  siemens.cateringportal.io")
    draw.rectangle([(0, H - 24), (W, H)], fill=SIEMENS_BLUE)
    bb = draw.textbbox((0, 0), txt, font=font)
    draw.text(((W - (bb[2]-bb[0])) // 2, H - 17), txt, font=font, fill=TEXT_WHITE)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    now       = datetime.now(timezone.utc)
    local_dt  = get_german_time(now)
    week_lbl  = get_week_label(now)
    kw        = local_dt.isocalendar()[1]
    jpg_path  = OUT_DIR / f"kantine_{week_lbl}.jpg"

    print(f"Target URL : {URL_MENU}")
    print(f"Week label : {week_lbl}  (KW {kw:02d})")

    week_data = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 900})

        print("Navigating to Cateringportal Speiseplan...")
        goto_with_fallback(page, URL_MENU)

        # Wait for any menu-like content
        for sel in ("table", ".menu-day", "[class*='meal']", "[class*='dish']",
                    "main", "#app", "#content"):
            try:
                page.wait_for_selector(sel, timeout=12000, state="visible")
                print(f"  Content visible: '{sel}'")
                break
            except Exception:
                pass
        else:
            print("  No selector matched – waiting 8 s for JS render.")
            page.wait_for_timeout(8000)

        page.wait_for_timeout(2000)
        print(f"Page title: {page.title()}")

        dismiss_consent(page)
        page.wait_for_timeout(1000)

        # Extract DOM data
        print("Extracting menu data from DOM...")
        raw = page.evaluate(DOM_EXTRACT_JS)
        print(f"  DOM strategy: {raw.get('strategy', '?')}")
        print(f"  Days found:   {len(raw.get('days', []))}")

        # Debug dump (visible in Actions log)
        if raw.get("days"):
            for d in raw["days"][:2]:
                print(f"  Day sample: {d['day']} -> {len(d['meals'])} meals")
                for m in d["meals"][:2]:
                    print(f"    {m}")
        else:
            print(f"  Raw text (first 500 chars): {raw.get('text','')[:500]}")

        browser.close()

    week_data = parse_dom_data(raw, local_dt)
    print(f"Parsed days: {list(week_data.keys())}")

    # Render table image
    img = render_table(week_data, kw, week_lbl, now)
    img.save(str(jpg_path), "JPEG", quality=92)
    print(f"Saved: {jpg_path} ({img.width}x{img.height})")

    # Cleanup old files
    existing = sorted(OUT_DIR.glob("kantine_*.jpg"))
    for old in existing[:-MAX_KEEP]:
        old.unlink()
        print(f"Removed old: {old}")


if __name__ == "__main__":
    main()
