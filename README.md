# siemens-kantine-photoframe

Weekly screenshot of the [Siemens Kantine Regensburg](https://siemens.cateringportal.io/menu/Regensburg/Mittagessen) lunch menu → RSS feed for the **Philips 8FF3WMI** digital photo frame.

Spiritual successor of [kicktipp-photoframe](https://github.com/basecore/kicktipp-photoframe), which served the WC 2026 Kicktipp table on the same photo frame.

## How it works

1. **Every Monday at 07:30 CEST** a GitHub Actions workflow runs `scripts/take_screenshot.py`
2. The script opens the Cateringportal page with [Playwright](https://playwright.dev/) (Chromium, headless), dismisses any GDPR banner and hides navigation chrome
3. The screenshot is resized/scaled to **800×600 px landscape** (optimised for the Philips 8FF3WMI)
4. A Siemens-blue status bar with the calendar week and timestamp is drawn at the bottom
5. The JPEG is committed to `docs/images/kantine_YYYY-Www.jpg` and uploaded via FTP to bplaced
6. `scripts/generate_rss.py` updates `docs/feed.xml` and `docs/feed.php`

## Latest screenshot

![Latest Speiseplan](docs/images/latest.jpg)

## RSS Feed URLs

| Format | URL |
|--------|-----|
| HTTPS (github.io) | `https://basecore.github.io/siemens-kantine-photoframe/feed.xml` |
| HTTP (bplaced, Philips frame) | `http://basecore.bplaced.net/feed.php` |

## Setup

### GitHub Secrets

| Secret | Description |
|--------|-------------|
| `BPLACED_FTP_PASSWORD` | FTP password for bplaced.net (inherited from kicktipp-photoframe) |
| `MAIL_USERNAME` | Gmail address for status notifications |
| `MAIL_PASSWORD` | Gmail app password |
| `CATERINGPORTAL_SID` | *(optional)* Session ID `ste_sid` – only needed if the page requires it |

### Self-hosted HAOS runner

The `upload-haos` job runs on your Home Assistant OS runner (`[self-hosted, haos]`) for FTP uploads – identical setup to kicktipp-photoframe.

## Dependencies

```
playwright
Pillow
```

Installed automatically by the workflow. Chromium is installed via `playwright install chromium`.
