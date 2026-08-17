"""台灣復健醫學會（pmr.org.tw）學術活動。

頁面是靜態 HTML（不需要 JS 渲染），每筆活動長這樣：

    <ul>
      <li class="text-dateO"><span>日期：</span>2026/08/22</li>
      <li><a href="active_info.asp?/4487.html">標題(共3時)(1.5點)</a>
          <div ...>主辦：某某醫院</div>
          <div ...>地點：新竹市北區...</div>
      </li>
      <li><span>點閱：</span>188</li>
    </ul>

分頁用 /active_news/active.asp?/2.html 這種形式，總頁數寫在「頁次：1 / 5」。
"""
from __future__ import annotations

import re
from typing import List

from bs4 import BeautifulSoup

from .base import (
    Event,
    clean_text,
    detect_categories,
    detect_region,
    get,
    parse_credits,
    parse_date,
    strip_prefix,
    warn,
)

NAME = "台灣復健醫學會"
BASE = "https://www.pmr.org.tw"
LIST_URL = BASE + "/active_news/active.asp"
MAX_PAGES = 20  # 安全上限，避免頁數解析錯誤時無限抓


def _page_url(page: int) -> str:
    return LIST_URL if page == 1 else "{}?/{}.html".format(LIST_URL, page)


def _total_pages(soup: BeautifulSoup) -> int:
    """從「頁次：1 / 5」抓總頁數；抓不到就當作只有 1 頁。

    命中 MAX_PAGES 上限時會發警告 —— 被截掉的活動如果無聲消失，
    網站上看起來就只是「這陣子課比較少」，沒人會發現漏抓。
    """
    text = clean_text(soup.get_text())
    match = re.search(r"頁次[：:]\s*\d+\s*/\s*(\d+)", text)
    if not match:
        return 1
    total = int(match.group(1))
    if total > MAX_PAGES:
        warn(
            "{}：共 {} 頁超過安全上限 {} 頁，只抓了前 {} 頁".format(
                NAME, total, MAX_PAGES, MAX_PAGES
            )
        )
        return MAX_PAGES
    return total


def _parse_rows(soup: BeautifulSoup) -> List[Event]:
    events = []
    for row in soup.select("div.list ul"):
        date_li = row.select_one("li.text-dateO")
        link = row.select_one("a[href]")
        if not date_li or not link:
            continue  # 表頭列（li.th）沒有日期也沒有連結

        iso_date = parse_date(date_li.get_text())
        if not iso_date:
            continue

        title = clean_text(link.get_text())
        if not title:
            continue

        organizer = ""
        location = ""
        for div in row.select("div"):
            text = clean_text(div.get_text())
            if text.startswith("主辦"):
                organizer = strip_prefix(text, "主辦")
            elif text.startswith("地點"):
                location = strip_prefix(text, "地點")

        href = link.get("href", "")
        if href.startswith("http"):
            url = href
        else:
            url = "{}/active_news/{}".format(BASE, href.lstrip("/"))

        events.append(
            Event(
                date=iso_date,
                title=title,
                organizer=organizer,
                location=location,
                credits=parse_credits(title),
                region=detect_region(location, organizer),
                source=NAME,
                url=url,
                categories=detect_categories(title, organizer),
            )
        )
    return events


def fetch() -> List[Event]:
    first = get(_page_url(1))
    first.encoding = first.apparent_encoding or "utf-8"
    soup = BeautifulSoup(first.text, "html.parser")

    events = _parse_rows(soup)
    for page in range(2, _total_pages(soup) + 1):
        resp = get(_page_url(page))
        resp.encoding = resp.apparent_encoding or "utf-8"
        events.extend(_parse_rows(BeautifulSoup(resp.text, "html.parser")))
    return events
