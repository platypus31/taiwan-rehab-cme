"""台灣兒童復健醫學會（tapedpmr.org.tw）學術活動。

列表頁 activity/index.asp 每筆長這樣：

    <ul class="list_td ...">
      <li ...><span>...<b>活動日期：</b></span>2026/10/04</li>
      <li ...><span>...<b>活動標題：</b></span><a href="info.asp?/233.html">標題(合辦；積分：1.5)</a></li>
    </ul>

列表頁沒有地點與主辦單位，要進詳情頁才有。列表是**日期新到舊**排序，
而且有 30 頁（大多是過期活動），所以只對「還沒發生的活動」進詳情頁，
一遇到整頁都是過期活動就停止翻頁 —— 這樣每次更新大約只打 10 次請求。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from bs4 import BeautifulSoup

from .base import (
    Event,
    clean_text,
    cutoff_iso,
    detect_categories,
    detect_region,
    get,
    parse_credits,
    parse_date,
    strip_prefix,
    warn,
)

NAME = "台灣兒童復健醫學會"
BASE = "https://www.tapedpmr.org.tw"
LIST_URL = BASE + "/activity/index.asp"
MAX_PAGES = 5  # 列表是新到舊，未來活動一定在最前面幾頁


def _page_url(page: int) -> str:
    return LIST_URL if page == 1 else "{}?/{}.html".format(LIST_URL, page)


def _parse_rows(soup: BeautifulSoup) -> List[Tuple[str, str, str]]:
    """回傳 (iso_date, title, url)，不含地點主辦（那要進詳情頁）。"""
    rows = []
    for row in soup.select("ul.list_td"):
        items = row.select("li")
        if len(items) < 2:
            continue
        link = row.select_one("a[href]")
        if not link:
            continue

        iso_date = parse_date(strip_prefix(items[0].get_text(), "活動日期"))
        title = clean_text(link.get_text())
        if not iso_date or not title:
            continue

        href = link.get("href", "")
        if href.startswith("http"):
            url = href
        else:
            url = "{}/activity/{}".format(BASE, href.lstrip("/"))
        rows.append((iso_date, title, url))
    return rows


def _fetch_detail(url: str) -> Tuple[str, str]:
    """從詳情頁抓 (地點, 主辦單位)。抓不到就回空字串，不讓單筆失敗拖垮整批。"""
    try:
        resp = get(url)
    except Exception as exc:  # noqa: BLE001 - 單筆失敗不拖垮整批，但要留下痕跡
        warn("{}：詳情頁抓取失敗，該筆缺地點與主辦單位（{}）".format(NAME, url))
        return "", ""
    resp.encoding = resp.apparent_encoding or "utf-8"
    text = clean_text(BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True))

    def grab(label: str, stops: List[str]) -> str:
        match = re.search(
            r"{}\s*[:：]\s*(.+?)(?:{})".format(label, "|".join(stops)), text
        )
        return clean_text(match.group(1)) if match else ""

    stops = ["活動日期", "活動地點", "主辦單位", "協辦單位", "瀏覽人次", "一、", "$"]
    return grab("活動地點", stops), grab("主辦單位", stops)


def fetch(with_detail: bool = True) -> List[Event]:
    # 用 build.py 同一個下界（不是 today），否則剛結束的活動會在來源層就被丟掉，
    # KEEP_PAST_DAYS 對這個來源等於失效。
    cutoff = cutoff_iso()
    events: List[Event] = []
    truncated = True  # 迴圈跑滿 MAX_PAGES 都沒 break = 可能還有沒抓到的頁

    for page in range(1, MAX_PAGES + 1):
        resp = get(_page_url(page))
        resp.encoding = resp.apparent_encoding or "utf-8"
        rows = _parse_rows(BeautifulSoup(resp.text, "html.parser"))
        if not rows:
            truncated = False
            break

        upcoming = [r for r in rows if r[0] >= cutoff]
        for iso_date, title, url in upcoming:
            location, organizer = _fetch_detail(url) if with_detail else ("", "")
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
                    categories=detect_categories(title, organizer) or ["兒童復健"],
                )
            )

        # 整頁都過期 = 後面只會更舊，停止翻頁
        if not upcoming:
            truncated = False
            break

    # 比照 pmr.py：命中安全上限要浮出來，不能默默少資料
    if truncated:
        warn(
            "{}：翻到第 {} 頁仍有未過期活動，已達安全上限，可能有活動未收錄".format(
                NAME, MAX_PAGES
            )
        )
    return events
