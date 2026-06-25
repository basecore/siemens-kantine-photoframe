#!/usr/bin/env python3
"""Takes a weekly screenshot of the Siemens Kantine Regensburg Speiseplan
   and saves it as JPEG.
   - No login required (public page).
   - Dismisses GDPR consent banner if present.
   - Output: 800x600 JPEG (landscape, optimised for Philips 8FF3WMI).
   - A date/time bar is drawn at the bottom of the image (deutsche Zeit).
"""
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont

URL_MENU    = os.environ.get(
    "CATERINGPORTAL_URL",
    "https://siemens.cateringportal.io/menu/Regensburg/Mittagessen"
)

# Optional session-ID (rotiert ggf.)
_SID = os.environ.get("CATERINGPORTAL_SID", "").strip()
if _SID:
    URL_MENU = f"{URL_MENU}?ste_sid={_SID}"

OUT_DIR     = Path("docs/images")
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_KEEP    = 8   # 2 Monate Wochenbilder

# Target frame resolution: landscape 800x600
FRAME_W, FRAME_H = 800, 600

# Viewport: breit genug für die Wochentabelle
VIEW_W, VIEW_H = 1100, 900


def get_german_time(utc_dt: datetime) -> datetime:
    """Convert UTC datetime to German local time (CET=UTC+1 / CEST=UTC+2)."""
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
    # Fallback: approximate DST
    year = utc_dt.year
    import calendar
    def last_sunday(y, month):
        last_day = calendar.monthrange(y, month)[1]
        d = datetime(y, month, last_day, tzinfo=timezone.utc)
        return d - timedelta(days=d.weekday() + 1 if d.weekday() != 6 else 0)
    cest_start = last_sunday(year, 3).replace(hour=1)
    cest_end   = last_sunday(year, 10).replace(hour=1)
    offset = timedelta(hours=2) if cest_start <= utc_dt < cest_end else timedelta(hours=1)
    return utc_dt + offset


def get_week_label(utc_dt: datetime) -> str:
    """Returns ISO-Woche als Label, z.B. '2026-W26'."""
    local_dt = get_german_time(utc_dt)
    iso_year, iso_week, _ = local_dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def dismiss_consent_banner(page):
    """Dismiss GDPR/cookie consent banner if present."""
    iframe_selectors = [
        "iframe[src*='privacy-mgmt.com']",
        "iframe[id*='sp_message']",
        "iframe[name*='sp_message']",
        "iframe.sp-iframe",
    ]
    for iframe_sel in iframe_selectors:
        try:
            frame = page.frame_locator(iframe_sel)
            btn = frame.locator(
                "button.sp_choice_type_11, "
                "button[title='Akzeptieren und weiter'], "
                "button[aria-label='Akzeptieren und weiter']"
            )
            if btn.count() > 0:
                btn.first.click(timeout=6000)
                print(f"Consent dismissed via iframe ({iframe_sel})")
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(1500)
                return True
        except Exception as e:
            print(f"iframe strategy ({iframe_sel}) failed: {e}")

    try:
        hidden = page.evaluate("""
            () => {
                const selectors = [
                    '#sp_message_container',
                    '[id^="sp_message_container"]',
                    '.sp-message-container',
                    '#notice',
                    '.message.type-modal',
                    '[class*="sp_message"]',
                ];
                let found = false;
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => {
                        el.style.display = 'none';
                        found = true;
                    });
                });
                document.documentElement.classList.remove('sp-message-open');
                document.body.classList.remove('sp-message-open');
                return found;
            }
        """)
        if hidden:
            print("Consent banner hidden via JavaScript DOM removal.")
            page.wait_for_timeout(800)
            return True
    except Exception as e:
        print(f"JS banner-hide failed: {e}")

    consent_selectors = [
        'button.sp_choice_type_11',
        'button[title="Akzeptieren und weiter"]',
        'button[aria-label="Akzeptieren und weiter"]',
        'button.action_button',
        'button[data-action="accept"]',
        'button#acceptAllButton',
        'a#acceptAllButton',
    ]
    for sel in consent_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click(timeout=5000)
                print(f"Consent banner dismissed with direct selector: {sel}")
                page.wait_for_load_state("networkidle", timeout=15000)
                page.wait_for_timeout(1500)
                return True
        except Exception as e:
            print(f"Direct selector {sel} failed: {e}")

    print("No consent banner found (already accepted or not shown).")
    return False


def hide_chrome(page):
    """Hide header, nav, footer, ads and any residual overlay elements."""
    page.evaluate("""
        () => {
            const hide = [
                'header', 'nav', 'footer',
                '.navigation', '#navigation',
                '.cookiebanner', '#cookiebanner',
                '.cookie-consent', '.cookiehinweis',
                '#notice', '.message.type-modal',
                '.sp-message-container', '#sp_message_container',
                '[id^="sp_message_container"]',
                '[class*="sp_message"]',
            ];
            hide.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => {
                    el.style.display = 'none';
                });
            });
            document.documentElement.classList.remove('sp-message-open');
            document.body.classList.remove('sp-message-open');
            window.scrollTo(0, 0);
        }
    """)
    page.wait_for_timeout(600)


def add_date_bar(img: Image.Image, utc_dt: datetime) -> Image.Image:
    """Draw a date/time bar at the bottom of the image (deutsche Zeit, 24h-Format)."""
    local_dt = get_german_time(utc_dt)
    week_label = get_week_label(utc_dt)
    date_str = f"KW {local_dt.isocalendar()[1]:02d} – {local_dt.strftime('%d.%m.%Y %H:%M Uhr')} – Siemens Kantine Regensburg"
    bar_h = 22
    bar_y = img.height - bar_h

    draw = ImageDraw.Draw(img)
    draw.rectangle([(0, bar_y), (img.width, img.height)], fill=(0, 0, 102))  # Siemens-Blau

    font = None
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for fp in font_paths:
        try:
            font = ImageFont.truetype(fp, 13)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), date_str, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (img.width - text_w) // 2
    text_y = bar_y + (bar_h - text_h) // 2
    draw.text((text_x, text_y), date_str, fill=(255, 255, 255), font=font)

    return img


def main():
    now       = datetime.now(timezone.utc)
    week_lbl  = get_week_label(now)
    png_path  = OUT_DIR / f"kantine_{week_lbl}.png"
    jpg_path  = OUT_DIR / f"kantine_{week_lbl}.jpg"

    print(f"Target URL : {URL_MENU}")
    print(f"Week label : {week_lbl}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": VIEW_W, "height": VIEW_H})

        print("Navigating to Cateringportal Speiseplan...")
        page.goto(URL_MENU, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        print(f"Page title: {page.title()}")

        page.wait_for_timeout(2000)
        dismiss_consent_banner(page)
        page.wait_for_timeout(1000)
        hide_chrome(page)

        page.screenshot(path=str(png_path), full_page=False)
        browser.close()

    # --- Resize / crop to exact 800x600 landscape for Philips 8FF3WMI ---
    img = Image.open(png_path).convert("RGB")
    w, h = img.size
    print(f"Raw screenshot size: {w}x{h}")

    # Scale down proportionally so content fits in 800x(600-22)
    content_h = FRAME_H - 22  # reserve bottom bar
    scale = min(FRAME_W / w, content_h / h)
    if scale < 1.0:
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        print(f"Scaled to: {new_w}x{new_h}")

    # Paste onto white canvas 800x600
    canvas = Image.new("RGB", (FRAME_W, FRAME_H), (255, 255, 255))
    paste_x = (FRAME_W - img.width) // 2
    paste_y = 0  # align to top
    canvas.paste(img, (paste_x, paste_y))
    img = canvas

    # Draw Siemens-blue date/KW bar at the bottom
    img = add_date_bar(img, now)

    img.save(str(jpg_path), "JPEG", quality=92)
    png_path.unlink()
    print(f"Saved: {jpg_path} ({img.width}x{img.height})")

    # Cleanup old files – keep only last MAX_KEEP
    existing = sorted(OUT_DIR.glob("kantine_*.jpg"))
    for old in existing[:-MAX_KEEP]:
        old.unlink()
        print(f"Removed old: {old}")


if __name__ == "__main__":
    main()
