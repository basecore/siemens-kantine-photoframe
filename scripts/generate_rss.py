#!/usr/bin/env python3
"""Generates RSS feeds for Philips PhotoFrame.

feed.php  - HTTP URLs via bplaced, .php extension -> for Philips PhotoFrame
            Uses the week filename (e.g. kantine_2026-W26.jpg) so that
            bplaced never serves a cached old image.
feed.xml  - HTTPS URLs via github.io, modern readers (uses week filename)

Only the LATEST image is included in all feeds so that the Philips 8FF3WMI
PhotoFrame does not display outdated screenshots.
"""
from pathlib import Path
from datetime import datetime, timezone

BASE_URL        = "https://basecore.github.io/siemens-kantine-photoframe"
BASE_URL_HTTP   = "http://basecore.bplaced.net"
IMAGES_URL      = f"{BASE_URL}/images"
IMAGES_URL_HTTP = f"{BASE_URL_HTTP}/images"
DOCS_DIR        = Path("docs")
IMAGES_DIR      = DOCS_DIR / "images"
RSS_PATH        = DOCS_DIR / "feed.xml"
PHP_PATH        = DOCS_DIR / "feed.php"

TITLE        = "Siemens Kantine Regensburg – Wochenspeiseplan"
FRAME_WIDTH  = 800
FRAME_HEIGHT = 600

def build_feed(images, use_http=False, php_header=False):
    base   = BASE_URL_HTTP   if use_http else BASE_URL
    imgurl = IMAGES_URL_HTTP if use_http else IMAGES_URL
    now_rfc = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

    lines = []
    if php_header:
        lines.append('<?php')
        lines.append('header("Content-Type: application/rss+xml; charset=utf-8");')
        lines.append('header("Cache-Control: no-cache, no-store, must-revalidate");')
        lines.append('header("Pragma: no-cache");')
        lines.append('header("Expires: 0");')
        lines.append('echo \'<?xml version="1.0" encoding="ISO-8859-1"?>\';')
        lines.append('?>')
    else:
        lines.append('<?xml version="1.0" encoding="ISO-8859-1"?>')

    lines += [
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
        '\t<channel>',
        f'\t\t<title>{TITLE}</title>',
        f'\t\t<link>{base}</link>',
        '\t\t<description></description>',
        f'\t\t<pubDate>{now_rfc}</pubDate>',
        f'\t\t<lastBuildDate>{now_rfc}</lastBuildDate>',
        f'\t\t<generator>{base}</generator>',
        '\t\t<image>',
        f'\t\t\t<url>{base}/images/icon.jpg</url>',
        f'\t\t\t<title>{TITLE}</title>',
        f'\t\t\t<link>{base}</link>',
        '\t\t</image>',
    ]

    for img_path in images:
        img_url = f"{imgurl}/{img_path.name}"
        # stem e.g. "kantine_2026-W26" -> week label "2026-W26"
        week_label = img_path.stem.replace("kantine_", "")
        try:
            # Parse ISO week to get Monday of that week
            year, week = week_label.split("-W")
            import datetime as dt_module
            monday = dt_module.datetime.fromisocalendar(int(year), int(week), 1)
            pub_rfc = monday.strftime("%a, %d %b %Y 06:00:00 +0000")
        except (ValueError, AttributeError):
            pub_rfc = now_rfc

        description = (
            f'&lt;img src=&quot;{img_url}&quot; '
            f'alt=&quot;Siemens Kantine Speiseplan {week_label}&quot;/&gt;'
        )

        lines += [
            '\t\t<item>',
            f'\t\t<title>Speiseplan {week_label}</title>',
            f'\t\t<link>{base}</link>',
            f'\t\t<description>{description}</description>',
            f'\t\t<pubDate>{pub_rfc}</pubDate>',
            f'\t\t<media:content url="{img_url}" type="image/jpeg" height="{FRAME_HEIGHT}" width="{FRAME_WIDTH}"/>',
            '\t\t</item>',
        ]

    lines += ['\t</channel>', '</rss>', '']
    return "\n".join(lines)

def main():
    all_images = sorted(IMAGES_DIR.glob("kantine_*.jpg"), reverse=True)
    if not all_images:
        print("No images found - nothing to do.")
        return

    images = [all_images[0]]
    print(f"Latest image: {images[0].name} (total available: {len(all_images)})")

    RSS_PATH.write_text(build_feed(images, use_http=False), encoding="utf-8")
    print(f"feed.xml  written: 1 item ({images[0].name})")

    PHP_PATH.write_text(build_feed(images, use_http=True, php_header=True), encoding="utf-8")
    print(f"feed.php  written: 1 item ({images[0].name})")

if __name__ == "__main__":
    main()
