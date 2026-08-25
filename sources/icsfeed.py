"""把 Event 清單算成 iCalendar（.ics）訂閱檔。

2026-08-26 定案：值班不一定每堂課都上得到，上不到的課進了日曆就只是雜訊，
所以訂閱是**選配**：要不要進日曆由使用者自己按，
單筆的「加入 Google 日曆」保留不動（單筆＝我要這一堂；訂閱＝以後都自動給我）。

🔴 **為什麼這支放在 sources/ 而不是 scripts/**：`scripts/` 沒有 `__init__.py`，
`scripts/selftest.py` 的 sys.path 技巧 import 不到 `scripts.*`。要讓格式邏輯
有回歸測試守著就得放在這裡。它是輸出格式不是資料來源，
命名用 icsfeed 跟真正的 source adapter（pmr／tapedpmr／lien）區隔。

🔴 **UID 必須跨 build 穩定**，否則訂閱端會把同一場活動當成新事件重複跳出來 ——
這是 ics 最常見也最惱人的坑，而且**不會有任何錯誤訊息**，只有訂閱的人被洗版。
這裡用的識別碼跟 `build.dedupe()` 判斷「兩筆是不是同一場」用的是**同一組欄位**
（日期 + `base.norm_title` 正規化後的標題），所以只要管線認為是同一場，UID 就一定一樣。
刻意不放 location／url／credits／region 這些會被來源網站修來修去的欄位。

⚠️ 已知限制：來源把活動**日期**改掉、或把標題改到連 `norm_title` 都正規化不成同一個
字串時，UID 會變，訂閱端會多一筆。這是可接受的 —— 那種程度的變動本來就該當成新活動。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

from .base import TAIPEI, Event, norm_title

# UID 的命名空間前綴。
#
# 🔴 **刻意不用 `<識別碼>@<網域>` 那個慣例寫法**，雖然 RFC 5545 建議 UID 長得像
# addr-spec。理由是那個形狀**跟 email 一模一樣** —— 姊妹站（泌尿科）的個資閘門
# 2026-08-25 就當場把 49 筆 UID 全部擋下來。那不是誤報而是「形狀真的沒辦法分辨」：
# 閘門看不出 `<sha1>@domain` 是 UID 還是信箱。
# **正確的處理是改我們自己的格式，不是把 data/*.ics 加進閘門白名單** ——
# 白名單會讓真的信箱哪天混進 .ics 也一起放行。
#
# 唯一性仍然成立：固定前綴（專案命名空間）+ sha1（內容雜湊）。
# ⚠️ 改動這個前綴等於讓所有既有訂閱者的事件全部重新產生一次，非必要不要動。
UID_PREFIX = "taiwan-rehab-cme"

# 訂閱端多久回來抓一次。資料一天更新一次（Actions 台灣 06:00），
# 所以 12 小時足夠；設更短只是讓別人的日曆 App 空跑。
REFRESH_INTERVAL = "PT12H"


def _fold(line: str) -> str:
    """RFC 5545 的折行：每行最多 75 個 octet，續行開頭補一個空白。

    🔴 必須以 **octet** 計算而不是字元 —— 中文一個字是 3 個 byte，
    用字元數折出來的行會超長，嚴格一點的訂閱端會整份拒收。
    同時不能把一個多位元組字元從中間切開，所以是一個字元一個字元疊上去。
    """
    if len(line.encode("utf-8")) <= 75:
        return line

    chunks: List[bytes] = []
    current = b""
    limit = 75
    for char in line:
        encoded = char.encode("utf-8")
        if len(current) + len(encoded) > limit:
            chunks.append(current)
            current = encoded
            limit = 74  # 續行被佔掉一個 byte 放開頭那個空白
        else:
            current += encoded
    if current:
        chunks.append(current)
    return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def _escape(text: str) -> str:
    """ics 的文字跳脫：反斜線、分號、逗號要跳脫，換行寫成 \\n。

    順序很重要 —— 反斜線一定要**先**換，不然後面補進去的跳脫符號會被二次跳脫。

    ⚠️ 三種換行（CRLF／LF／單獨的 CR）要**先統一**再一起轉成跳脫符。
    直接把單獨的 `\\r` 砍掉的話，用舊式 Mac 換行的來源會整個斷行語意消失
    （兩行被黏成一行），而且不會有任何提示。
    """
    out = str(text or "")
    out = out.replace("\\", "\\\\")
    out = out.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    out = out.replace(";", "\\;").replace(",", "\\,")
    return out


def event_uid(event: Event) -> str:
    """跨 build 穩定的 UID。

    用 `base.norm_title` 而不是自己複製一份 —— `build.dedupe()` 也用它，
    兩邊共用同一支才能保證「管線認為是同一場」與「訂閱端認為是同一場」永遠一致。
    識別欄位必須跟 `dedupe()` 的 key 完全對齊（目前是 日期 + 正規化標題）。
    """
    identity = "|".join([event.date or "", norm_title(event.title)])
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()
    return "{}-{}".format(UID_PREFIX, digest)


def _compact(iso_date: str) -> str:
    return (iso_date or "").replace("-", "")


def _plus_one_day(iso_date: str) -> str:
    """整天事件的 DTEND 是**不含**的，所以要 +1 天。"""
    parsed = datetime.strptime(iso_date, "%Y-%m-%d") + timedelta(days=1)
    return parsed.strftime("%Y%m%d")


def _event_lines(event: Event, dtstamp: str) -> List[str]:
    """一筆 VEVENT。

    🔴 **全部都是整天事件**，這不是偷懶：三個來源的公告都只給日期不給起訖時間
    （`Event` 連 `time` 欄位都沒有）。前端的「加入 Google 日曆」也是同一套處理，
    兩邊必須一致，否則同一場活動用兩種方式加進日曆會長得不一樣。
    ⚠️ 不要改成從標題正則抓時間 —— 標題裡的「(共4時)」是**時數**不是上課時段，
    抓下去會產生看起來精確、實際上錯誤的時段。
    """
    lines = ["BEGIN:VEVENT", "UID:" + event_uid(event), "DTSTAMP:" + dtstamp]
    lines.append("DTSTART;VALUE=DATE:" + _compact(event.date))
    lines.append("DTEND;VALUE=DATE:" + _plus_one_day(event.date))

    lines.append("SUMMARY:" + _escape(event.title))
    if event.location:
        lines.append("LOCATION:" + _escape(event.location))
    if event.url:
        lines.append("URL:" + _escape(event.url))

    description = "\n".join(
        part
        for part in [
            "主辦：" + event.organizer if event.organizer else "",
            "積分：{:g} 點".format(event.credits) if event.credits is not None else "積分：未標示",
            "分類：" + "、".join(event.categories) if event.categories else "",
            "來源：" + event.source if event.source else "",
            "簡章與報名：" + event.url if event.url else "",
            "（時間為整天，實際起訖請看主辦單位公告）",
        ]
        if part
    )
    lines.append("DESCRIPTION:" + _escape(description))

    lines.append("END:VEVENT")
    return lines


def render(events: Iterable[Event], calendar_name: str, dtstamp: Optional[str] = None) -> str:
    """算出一份完整的 .ics 內容（含 CRLF 結尾，符合 RFC 5545）。

    dtstamp 傳 build 的 updated_at，讓「資料沒更新時檔案內容不變」——
    每次都塞當下時間會讓 git 每天產生無意義的 diff。
    """
    if dtstamp is None:
        dtstamp = utc_stamp()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//platypusbot//taiwan-rehab-cme//ZH-TW",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:" + _escape(calendar_name),
        "X-WR-TIMEZONE:Asia/Taipei",
        "REFRESH-INTERVAL;VALUE=DURATION:" + REFRESH_INTERVAL,
        "X-PUBLISHED-TTL:" + REFRESH_INTERVAL,
        # 台灣沒有日光節約時間，固定 +0800，所以 VTIMEZONE 只需要一段 STANDARD。
        # 目前全部是整天事件用不到 TZID，但檔頭帶著它，日曆 App 才知道
        # 「這份行事曆的自然時區是台北」（X-WR-TIMEZONE 不是所有端都認）。
        "BEGIN:VTIMEZONE",
        "TZID:Asia/Taipei",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        "TZOFFSETFROM:+0800",
        "TZOFFSETTO:+0800",
        "TZNAME:CST",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]

    for event in events:
        lines.extend(_event_lines(event, dtstamp))

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def utc_stamp(iso_datetime: Optional[str] = None) -> str:
    """把 build 的 updated_at（帶 +08:00）換成 ics 的 DTSTAMP 形式（UTC，結尾 Z）。

    傳 None 或格式不對就退回「現在」。DTSTAMP 規範上必須是 UTC。
    """
    try:
        parsed = datetime.fromisoformat(iso_datetime) if iso_datetime else None
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        parsed = datetime.now(TAIPEI)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI)
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
