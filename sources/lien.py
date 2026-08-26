"""財團法人台北市連倚南教授復健醫學教育基金會（lien.foundation）年度課程。

這個基金會的課程**不會出現在台灣復健醫學會的活動列表**（2026-08-20 實查，
超音波工作坊／義肢裝具研習會／癌症復健研討會在 pmr.org.tw 一筆都沒有），
所以它是新增涵蓋度而不是重複來源。

站台是 WordPress，靜態 HTML（不需要 JS 渲染，也沒有人機驗證）。
權威清單是「年度行事曆」那一頁，一筆一個 <li>：

    <h2>2026年行事曆</h2>
    <ul class="wp-block-list">
      <li>10月04日 <a href="https://lien.foundation/lower_limb_prosthese/">下肢義肢研習會</a> (2026.8更新)</li>
      ...
    </ul>

**年份只寫在標題**（列表裡只有「10月04日」），所以年份從 <h2> 抓，不能寫死。
列表沒有地點與積分，那些在各課程的內頁；只對還沒發生的活動進內頁，
所以每次更新大約只多打 2-4 個請求。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from .base import (
    Event,
    SourceError,
    clean_text,
    cutoff_iso,
    detect_categories,
    detect_region,
    get,
    parse_date,
    scrub_contacts,
    today_taipei,
    warn,
)

NAME = "連倚南教授復健醫學教育基金會"
# 內頁沒寫主辦單位時的預設值。基金會自己就是主辦方，這不是推測。
ORGANIZER = "財團法人台北市連倚南教授復健醫學教育基金會"
BASE = "https://lien.foundation"
CALENDAR_URL = BASE + "/yearlyplan/"

_YEAR_RE = re.compile(r"(\d{4})\s*年\s*行事曆")
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")

# 行事曆上的維護註記，例如「(2026.3更新)」「(2026.4.9更新）」
# （括號左右全形半形會混用，所以兩邊各自吃兩種）。
_NOTE_RE = re.compile(r"[(（][^)）]*更新[^)）]*[)）]")

# 「截止申請－優秀論文獎」這類是獎助申請期限不是課程；
# 「(因颱風取消)」的梯次也不該出現在站上（她點進去只會撲空）。
_SKIP_KEYWORDS = ("取消", "截止申請")

# 內頁的欄位都長成 <p><strong>標　　籤</strong>　值</p>，
# clean_text 會把全形空格壓成半形，所以標籤字之間要允許空白。
_DETAIL_FIELDS = [
    ("date", re.compile(r"^(?:日\s*期|時\s*間)\s*[:：]?\s*(.+)$")),
    ("location", re.compile(r"^地\s*點\s*[:：]?\s*(.+)$")),
    ("organizer", re.compile(r"^主\s*辦\s*單\s*位\s*[:：]?\s*(.+)$")),
    ("credits", re.compile(r"^(?:教\s*育\s*積\s*分|積\s*分\s*認\s*證)\s*[:：]?\s*(.+)$")),
]

# 積分那一行會同時列好幾個學會（「台灣復健醫學會2.5、物理治療學會4.9…」），
# 這個站要的是復健醫學會的點數，所以綁著學會名抓，不能抓行內第一個數字。
# 「台灣復健醫學會積分申請中」這種還沒核下來的寫法抓不到數字 → 歸「未標示」，正確。
_PMR_CREDIT_RE = re.compile(r"台灣復健醫學會\s*(?:學分|積分)?\s*(\d+(?:\.\d+)?)")

# (內頁日期, 地點, 主辦單位, 積分)
Detail = Tuple[Optional[str], str, str, Optional[float]]


def _absolute(href: str) -> str:
    if href.startswith("http"):
        return href
    return "{}/{}".format(BASE, href.lstrip("/"))


def _entry_content(html: str) -> Optional[BeautifulSoup]:
    """WordPress 的內文區塊。側欄與選單都在它外面，不會被掃到。"""
    return BeautifulSoup(html, "html.parser").select_one("div.entry-content")


def _fetch_detail(url: str) -> Detail:
    """從課程內頁抓日期／地點／主辦／積分。抓不到就回空值，不讓單筆失敗拖垮整批。"""
    try:
        resp = get(url)
    except Exception as exc:  # noqa: BLE001 - 單筆失敗不拖垮整批，但要留下痕跡
        warn("{}：課程頁抓取失敗，該筆缺地點與積分（{} — {}）".format(NAME, url, exc))
        return None, "", "", None
    resp.encoding = resp.apparent_encoding or "utf-8"

    content = _entry_content(resp.text)
    if content is None:
        # 內頁改版時該筆會悄悄變成沒有地點與積分。資料默默變空一律要留痕跡。
        warn("{}：課程頁找不到內文區塊，該筆缺地點與積分（{}）".format(NAME, url))
        return None, "", "", None

    found: Dict[str, str] = {}
    for para in content.select("p"):
        # 不加分隔符：欄位值是緊接在 <strong>標籤</strong> 後面的文字節點，
        # 塞空白進去只會讓「合<mark>辦單位</mark>」這種拆開的標籤對不上。
        text = clean_text(para.get_text())
        if not text:
            continue
        for field, pattern in _DETAIL_FIELDS:
            if field in found:
                continue  # 同一個標籤只認第一次出現（下面還有退費日期之類的雜訊）
            match = pattern.match(text)
            if not match:
                continue
            value = clean_text(match.group(1))
            if field == "date" and not parse_date(value):
                continue  # 議程表頭「時 間 講 題 講 者」也以「時間」開頭，不是日期
            found[field] = value

    location = found.get("location", "")
    if "。" in location:
        # 「台北市常德街1號 。 早上：復健大樓4四樓415教室。下午：…」
        # 第一句就是地址，後面是教室分配，卡片上不需要。
        head = location.split("。", 1)[0].strip()
        if head:
            location = head

    credits = None
    match = _PMR_CREDIT_RE.search(found.get("credits", ""))
    if match:
        value = float(match.group(1))
        if 0 < value <= 20:  # 同 base.parse_credits 的合理上界
            credits = value

    return parse_date(found.get("date", "")), location, found.get("organizer", ""), credits


def fetch(with_detail: bool = True) -> List[Event]:
    resp = get(CALENDAR_URL)
    resp.encoding = resp.apparent_encoding or "utf-8"
    content = _entry_content(resp.text)
    if content is None:
        raise SourceError("{}：行事曆頁找不到內文區塊（頁面可能改版）".format(NAME))

    # 年份只寫在標題（「2026年行事曆」），列表裡只有「10月04日」。
    #
    # 這裡刻意**收集所有年份標題、每份清單各用自己標題的年份**，而不是挑一個年份
    # 套用到全頁。挑一個就得回答「挑哪個才對」：挑第一個，日後頁面在前面插一個
    # 「歷年行事曆」封存區塊就整批算錯年；挑最大值，明年他們貼出「2027年行事曆」
    # 預告時又會把當年度剩下的課整批丟掉。逐區塊各用各的年份則兩種情況都成立 ——
    # 舊年度的活動自然會被下面的 cutoff 濾掉，不必靠猜。
    # 年份錯是**每一筆日期都錯**，而且不會有任何症狀提醒我們，值得這樣寫。
    sections = []
    for node in content.find_all(["h1", "h2", "h3", "h4", "p"]):
        match = _YEAR_RE.search(clean_text(node.get_text()))
        if match:
            sections.append((int(match.group(1)), node))
    if not sections:
        # 年份是每一筆日期的來源，抓不到就整包不可信 —— 寧可整源失敗讓 errors 浮出來，
        # 也不要猜一個年份產生一堆日期錯誤的活動。
        raise SourceError("{}：行事曆頁抓不到年份標題（頁面可能改版）".format(NAME))

    if max(year for year, _ in sections) < today_taipei().year:
        # 跨年後對方還沒換頁 = 所有活動都會被判過期而消失。
        # 這種「資料默默變空」正是 warn 要攔的情況。
        warn(
            "{}：行事曆頁最新只到 {} 年，今年的課程可能尚未公告".format(
                NAME, max(year for year, _ in sections)
            )
        )

    cutoff = cutoff_iso()
    cache: Dict[str, Detail] = {}  # 同一個課程頁會被多梯次共用（例如診斷與注射班兩梯）
    events: List[Event] = []
    items: List[Tuple[int, "BeautifulSoup"]] = []

    for year, heading in sections:
        # 只掃標題後面緊接的那一份清單。WordPress 的 ul.wp-block-list 是通用 class，
        # 頁面其他區塊（報名須知／FAQ）用同一個 class 又剛好寫到「X月X日」時，
        # 無範圍限定的 select 會把它當成課程收進來。
        calendar_list = heading.find_next_sibling("ul")
        if calendar_list is None:
            continue
        for node in calendar_list.find_all("li", recursive=False):
            items.append((year, node))
    if not items:
        raise SourceError("{}：年份標題後面找不到活動清單（頁面可能改版）".format(NAME))

    for year, item in items:
        # 同樣不加分隔符：原始碼裡日期與標題之間本來就有空白，
        # 多塞一個會讓「『加開』骨骼肌肉…」中間裂開。
        text = clean_text(item.get_text())
        if not text or any(word in text for word in _SKIP_KEYWORDS):
            continue

        month_day = _MONTH_DAY_RE.search(text)
        if not month_day:
            continue  # 「全年申請 出席國際學術會議發表論文獎助」這類沒有日期
        try:
            iso_date = date(year, int(month_day.group(1)), int(month_day.group(2))).isoformat()
        except ValueError:
            continue

        # 用 build.py 同一個下界（不是 today），否則剛結束的活動會在來源層就被丟掉，
        # KEEP_PAST_DAYS 對這個來源等於失效。順帶省掉過期活動的內頁請求。
        if iso_date < cutoff:
            continue

        title = clean_text(_NOTE_RE.sub("", text[: month_day.start()] + text[month_day.end() :]))
        if not title:
            continue

        link = item.select_one("a[href]")
        url = _absolute(link["href"]) if link else CALENDAR_URL

        location = ""
        organizer = ""
        credits: Optional[float] = None
        if with_detail and link is not None:
            if url not in cache:
                cache[url] = _fetch_detail(url)
            detail_date, detail_location, detail_organizer, detail_credits = cache[url]
            # 內頁常常還停在上一梯（行事曆更新了、內頁沒跟上：癌症復健的頁面現在寫的還是
            # 2024 年那場），連結本身也可能掛錯課（2026 行事曆的 1/31 就指到別堂課的頁面）。
            # 所以只有內頁日期跟行事曆對得上才採用，對不上寧可留白 ——
            # 地點標錯會讓人白跑一趟，比沒標更糟（同 base.detect_region 的取捨）。
            if detail_date == iso_date:
                location = detail_location
                organizer = detail_organizer
                credits = detail_credits

        organizer = organizer or ORGANIZER
        # 內頁的地點／主辦是自由文字，承辦人的分機／信箱常寫在同一段。
        # 在進 Event 之前挖掉（見 base.scrub_contacts）——
        # 等到 scripts/pii-scan.sh 才發現的話，髒資料已經在 events.json 裡了。
        location = scrub_contacts(location)
        organizer = scrub_contacts(organizer) or ORGANIZER
        events.append(
            Event(
                date=iso_date,
                title=title,
                organizer=organizer,
                location=location,
                credits=credits,
                region=detect_region(location, organizer),
                source=NAME,
                url=url,
                categories=detect_categories(title),
            )
        )

    return events
