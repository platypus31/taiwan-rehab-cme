#!/usr/bin/env python3
"""不連網的自我測試：守住那些「壞掉也不會有錯誤訊息」的規則。

跑法：`python3 scripts/selftest.py`（CI 在抓資料之前會先跑一次）。

這裡刻意只測**沉默失效**的東西 —— 抓網頁的部分壞掉會有 [FAIL] 印出來，
但 .ics 的格式錯誤不會：訂閱端只會安靜地把事件重複跳出來、或整份拒收，
沒有人會收到通知。所以這些規則必須有測試守著。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import icsfeed  # noqa: E402
from sources.base import Event, norm_title  # noqa: E402

FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print("[ok] {}".format(name))
    else:
        FAILURES.append(name)
        print("[FAIL] {}{}".format(name, "：" + detail if detail else ""), file=sys.stderr)


def sample(**overrides) -> Event:
    fields = dict(
        date="2026-08-22",
        title="棒球運動傷害與運動處方115.08.22(共4時)(2點)",
        organizer="屏東基督教醫院",
        location="六樓簡報室",
        credits=2.0,
        region="南部",
        source="台灣復健醫學會",
        url="https://www.pmr.org.tw/active_news/active_info.asp?/4481.html",
        categories=["運動醫學"],
    )
    fields.update(overrides)
    return Event(**fields)


def test_uid_stable() -> None:
    """UID 只能跟著「哪一場活動」變，不能跟著會被官網改來改去的欄位變。

    🔴 這條壞掉的症狀是：訂閱的人每天在日曆上看到同一場活動重複冒出來，
    而且沒有任何錯誤訊息。所以地點／網址／積分／分類／主辦改了，UID 必須不變。
    """
    base = icsfeed.event_uid(sample())
    for field, value in [
        ("location", "改成別的會議室"),
        ("url", "https://example.org/other"),
        ("credits", 3.0),
        ("categories", ["骨骼肌肉", "疼痛"]),
        ("organizer", "換一個主辦"),
        ("source", "台灣兒童復健醫學會"),
        ("region", "北部"),
    ]:
        check(
            "UID 不隨 {} 改變".format(field),
            icsfeed.event_uid(sample(**{field: value})) == base,
            "{} 改了之後 UID 就變了".format(field),
        )

    check(
        "UID 隨日期改變",
        icsfeed.event_uid(sample(date="2026-08-23")) != base,
        "換了日期應該算不同活動",
    )
    # 標題只差在括號註記（積分／時數）時，dedupe 認定是同一場，UID 也必須一樣
    check(
        "UID 與 dedupe 判準一致（括號註記不算差異）",
        icsfeed.event_uid(sample(title="棒球運動傷害與運動處方 115.08.22 (2點)")) == base,
        "dedupe 認為同一場，UID 卻不同 —— 兩邊判準已經分岔",
    )
    check(
        "UID 不長得像 email",
        "@" not in base,
        "UID 用 <id>@<domain> 會被個資閘門當成信箱擋下（姊妹站踩過）",
    )


def test_uid_matches_dedupe_key() -> None:
    """UID 的識別欄位必須跟 build.dedupe() 的 key 完全對齊。

    這條是上一組的「白箱版」：直接比對兩邊算出來的識別字串，
    這樣哪天有人改了 dedupe 的 key 卻忘了改 UID，這裡會紅。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build  # noqa: E402

    a = sample()
    b = sample(title="棒球運動傷害與運動處方，115.08.22（共4時）（2點）", location="別的地方")
    same_by_dedupe = len(build.dedupe([a, b])) == 1
    same_by_uid = icsfeed.event_uid(a) == icsfeed.event_uid(b)
    check(
        "dedupe 與 UID 對同一組資料的判斷一致",
        same_by_dedupe == same_by_uid,
        "dedupe={} 但 UID={}".format(same_by_dedupe, same_by_uid),
    )


def test_all_day_dtend() -> None:
    """整天事件的 DTEND 是**不含**的，所以要 +1 天。

    少了這一天，日曆上的活動會顯示成「前一天結束」——
    典型的 off-by-one，看起來只是小事，但使用者會以為活動在別天。
    """
    text = icsfeed.render([sample()], "測試")
    check("DTSTART 是活動當天", "DTSTART;VALUE=DATE:20260822" in text, text[:400])
    check("DTEND 是隔天（不含）", "DTEND;VALUE=DATE:20260823" in text, text[:400])

    # 跨月／跨年邊界也要對
    text = icsfeed.render([sample(date="2026-12-31")], "測試")
    check("跨年邊界 DTEND 正確", "DTEND;VALUE=DATE:20270101" in text, text[:400])


def test_folding_by_octet() -> None:
    """折行必須以 octet 計算，且不可把多位元組字元切成兩半。

    🔴 中文一個字 3 bytes：用字元數折出來的行會超過 75 octet，
    嚴格一點的訂閱端會**整份拒收**。
    """
    long_title = "復健" * 60  # 360 bytes，遠超過一行上限
    text = icsfeed.render([sample(title=long_title)], "測試")

    for line in text.split("\r\n"):
        check_len = len(line.encode("utf-8"))
        if check_len > 75:
            check("每行不超過 75 octet", False, "有一行 {} bytes：{}".format(check_len, line[:40]))
            return
    check("每行不超過 75 octet", True)

    # 折過的內容要能還原回原標題（沒有把字元切壞、也沒有掉字）
    unfolded = text.replace("\r\n ", "")
    check("折行後可還原（沒有切壞多位元組字元）", "SUMMARY:" + long_title in unfolded)
    check("續行以一個空白開頭", "\r\n " in text, "長標題應該要被折行")


def test_escaping() -> None:
    """逗號／分號／反斜線要跳脫，換行要變成 \\n。反斜線必須先跳脫，否則會二次跳脫。"""
    text = icsfeed.render([sample(title="A,B;C\\D", location="x\ny")], "測試")
    unfolded = text.replace("\r\n ", "")
    check("逗號與分號有跳脫", "SUMMARY:A\\,B\\;C\\\\D" in unfolded, unfolded[:500])
    check("換行變成 \\n 不是真的換行", "LOCATION:x\\ny" in unfolded, unfolded[:500])


def test_lone_cr_preserved() -> None:
    """三種換行都要變成 `\\n` 跳脫符，不能有任何一種被默默砍掉。

    單獨的 `\\r`（舊式 Mac 換行）若直接砍掉，兩行會被黏成一行，
    而且不會有任何提示 —— 是典型的沉默失效。
    """
    for label, raw in [("LF", "a\nb"), ("CRLF", "a\r\nb"), ("單獨 CR", "a\rb")]:
        text = icsfeed.render([sample(location=raw)], "測試").replace("\r\n ", "")
        check(
            "{} 換行轉成 \\n 跳脫符".format(label),
            "LOCATION:a\\nb" in text,
            "{} 沒有被正確處理".format(label),
        )


def test_empty_calendar_not_written() -> None:
    """沒有活動時不產出空殼行事曆，而且上一次的檔案要刪掉。

    🔴 一份零 VEVENT 的 .ics 在訂閱端顯示的是「壞掉的行事曆」而不是「目前沒有課」；
    留著舊檔更糟 —— 訂閱的人會永遠收到不再更新的舊資料，完全沒有徵兆。
    """
    import shutil
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build  # noqa: E402

    tmpdir = Path(tempfile.mkdtemp())
    original = build.OUTPUT
    try:
        build.OUTPUT = tmpdir / "events.json"

        # 先用有資料的一輪產檔
        feeds = build._write_feeds([sample()], "2026-08-20T23:09:13+08:00")
        check("有活動時產出 all.ics", (tmpdir / "all.ics").exists())
        check("有活動時產出該地區的檔", (tmpdir / "region-south.ics").exists())
        check("feeds 含「全部」與該地區", feeds.get("") == "all.ics" and feeds.get("南部"))
        check("沒有活動的地區不產檔", not (tmpdir / "region-north.ics").exists())

        # 再跑一輪 0 筆：兩種檔案都要被刪掉，feeds 要是空的
        feeds = build._write_feeds([], "2026-08-20T23:09:13+08:00")
        check("0 筆時不留下空的 all.ics", not (tmpdir / "all.ics").exists())
        check("0 筆時刪掉上一輪的地區檔", not (tmpdir / "region-south.ics").exists())
        check("0 筆時 feeds 是空的（前端會藏起整區）", feeds == {})
    finally:
        build.OUTPUT = original
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_stable_across_builds() -> None:
    """活動一場都沒變的那天，訂閱檔必須**位元組完全相同**。

    🔴 DTSTAMP 取自 build 的 `updated_at`，那是每次 build 的當下時間，天天不一樣。
    若照寫，七份訂閱檔會在沒有新課的日子照樣天天產生 diff，
    git 歷史被無意義的 commit 洗版，也分不出「今天真的有新課」還是「只是時間戳跳動」。
    這條測試就是守這件事 —— 症狀只會出現在 CI 的每日 commit 裡，本機跑一次看不出來。
    """
    import shutil
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build  # noqa: E402

    tmpdir = Path(tempfile.mkdtemp())
    original = build.OUTPUT
    try:
        build.OUTPUT = tmpdir / "events.json"
        rows = [sample(), sample(date="2026-09-01", title="另一場課")]

        build._write_feeds(rows, "2026-08-26T06:00:00+08:00")
        first = (tmpdir / "all.ics").read_bytes()

        # 隔天再跑一次，資料一模一樣、只有 build 時間不同
        build._write_feeds(rows, "2026-08-27T06:00:00+08:00")
        second = (tmpdir / "all.ics").read_bytes()
        check("資料沒變時訂閱檔位元組相同（不會天天產生 diff）", first == second)

        # 真的多了一場課 → 檔案必須更新
        rows.append(sample(date="2026-09-15", title="新加的課"))
        build._write_feeds(rows, "2026-08-28T06:00:00+08:00")
        third = (tmpdir / "all.ics").read_bytes()
        check("真的有新活動時訂閱檔會更新", third != second)
        check("新活動有進到檔案裡", "新加的課" in third.decode("utf-8"))

        # 只有 DTSTAMP 不同時 _write_ics 要回報「沒寫」
        same = icsfeed.render(rows, "復健醫學 繼續教育活動",
                              dtstamp=icsfeed.utc_stamp("2026-09-09T06:00:00+08:00"))
        check("_write_ics 對只差 DTSTAMP 的內容回報未寫入",
              build._write_ics(tmpdir / "all.ics", same) is False)
    finally:
        build.OUTPUT = original
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_degraded_build_does_not_touch_feeds() -> None:
    """有來源抓失敗的那輪，訂閱檔一份都不能動。

    🔴 這條重現的是真實事故：2026-08-26 06:20 的自動更新，三個來源掛了兩個
    （522 與連線逾時），events.json 從 60 筆掉到 4 筆。網站有錯誤提示可以顯示，
    但 .ics 沒有任何地方放得下那條提示 —— 訂閱者的行事曆會安靜地被清空，
    連「這是暫時的」都不知道。所以寧可給舊資料，也不要動。
    """
    import shutil
    import tempfile

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build  # noqa: E402

    tmpdir = Path(tempfile.mkdtemp())
    original = build.OUTPUT
    try:
        build.OUTPUT = tmpdir / "events.json"
        healthy = [sample(), sample(date="2026-09-01", title="北部的課", region="北部")]
        build._write_feeds(healthy, "2026-08-26T06:00:00+08:00")
        before = {p.name: p.read_bytes() for p in tmpdir.glob("*.ics")}
        check("正常那輪產出三份檔（全部＋南部＋北部）", len(before) == 3, str(sorted(before)))

        # 隔天：來源掛掉只剩一筆（真實事故的形狀）
        feeds = build._write_feeds(
            [sample(date="2026-10-04", title="剩下的孤兒課", region="北部")],
            "2026-08-27T06:00:00+08:00",
            degraded=True,
        )
        after = {p.name: p.read_bytes() for p in tmpdir.glob("*.ics")}
        check("來源失敗時訂閱檔完全沒被改動", before == after)
        check("來源失敗時沒有任何地區檔被刪掉", set(before) == set(after), str(sorted(after)))
        check("feeds 仍對應磁碟上真實存在的檔案",
              set(feeds.values()) == set(after),
              "feeds={} 檔案={}".format(sorted(feeds.values()), sorted(after)))

        # 對照組：同一批縮水資料但來源都正常 → 這時就該照實反映（刪掉南部）
        build._write_feeds(
            [sample(date="2026-10-04", title="剩下的孤兒課", region="北部")],
            "2026-08-28T06:00:00+08:00",
        )
        normal = {p.name for p in tmpdir.glob("*.ics")}
        check("來源正常時資料真的變少就照實反映", "region-south.ics" not in normal, str(sorted(normal)))
    finally:
        build.OUTPUT = original
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_crlf_and_envelope() -> None:
    """換行是 CRLF，而且沒有落單的 LF。檔頭檔尾要完整。"""
    text = icsfeed.render([sample()], "復健醫學 繼續教育活動（南部）")
    check("以 BEGIN:VCALENDAR 開頭", text.startswith("BEGIN:VCALENDAR\r\n"))
    check("以 END:VCALENDAR 結尾", text.endswith("END:VCALENDAR\r\n"))
    check("沒有落單的 LF", text.replace("\r\n", "") .find("\n") == -1)
    check("有台北時區宣告", "TZID:Asia/Taipei" in text and "X-WR-TIMEZONE:Asia/Taipei" in text)
    check("行事曆名稱有寫進去", "X-WR-CALNAME:復健醫學 繼續教育活動（南部）" in text.replace("\r\n ", ""))
    check("事件數量正確", text.count("BEGIN:VEVENT") == 1 and text.count("END:VEVENT") == 1)


def test_dtstamp_stable() -> None:
    """同一個 updated_at 要算出同一個 DTSTAMP（否則 git 每天都會有無意義的 diff），
    而且必須換算成 UTC（規範要求）。"""
    a = icsfeed.render([sample()], "測試", dtstamp=icsfeed.utc_stamp("2026-08-20T23:09:13+08:00"))
    b = icsfeed.render([sample()], "測試", dtstamp=icsfeed.utc_stamp("2026-08-20T23:09:13+08:00"))
    check("同一個 updated_at 產出相同內容", a == b)
    check(
        "DTSTAMP 換算成 UTC 並以 Z 結尾",
        "DTSTAMP:20260820T150913Z" in a,
        "台北 23:09 應該是 UTC 15:09",
    )
    check("updated_at 壞掉時不會炸", len(icsfeed.utc_stamp("not-a-date")) == 16)


def test_credits_rendering() -> None:
    """積分要寫成人看得懂的樣子，未標示也要寫出來（不能留空讓人以為是 0 點）。"""
    text = icsfeed.render([sample(credits=2.0)], "測試").replace("\r\n ", "")
    check("整數積分不顯示小數點", "積分：2 點" in text, text[:600])
    text = icsfeed.render([sample(credits=1.5)], "測試").replace("\r\n ", "")
    check("小數積分保留小數", "積分：1.5 點" in text, text[:600])
    text = icsfeed.render([sample(credits=None)], "測試").replace("\r\n ", "")
    check("未標示積分寫成「未標示」", "積分：未標示" in text, text[:600])


def test_norm_title_shared() -> None:
    """norm_title 只能有一份實作（base），icsfeed 必須是用它而不是自己複製一份。"""
    check(
        "icsfeed 用的是 base.norm_title",
        icsfeed.norm_title is norm_title,
        "icsfeed 自己複製了一份 norm_title，兩邊遲早會分岔",
    )


def main() -> int:
    test_uid_stable()
    test_uid_matches_dedupe_key()
    test_all_day_dtend()
    test_folding_by_octet()
    test_escaping()
    test_lone_cr_preserved()
    test_empty_calendar_not_written()
    test_stable_across_builds()
    test_degraded_build_does_not_touch_feeds()
    test_crlf_and_envelope()
    test_dtstamp_stable()
    test_credits_rendering()
    test_norm_title_shared()

    if FAILURES:
        print("\n{} 項未通過：{}".format(len(FAILURES), "、".join(FAILURES)), file=sys.stderr)
        return 1
    print("\n全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
