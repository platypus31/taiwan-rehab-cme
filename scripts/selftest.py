#!/usr/bin/env python3
"""不連網的自我測試：守住那些「壞掉也不會有錯誤訊息」的規則。

跑法：`python3 scripts/selftest.py`（CI 在抓資料之前會先跑一次）。

這裡刻意只測**沉默失效**的東西 —— 抓網頁的部分壞掉會有 [FAIL] 印出來，
但 .ics 的格式錯誤不會：訂閱端只會安靜地把事件重複跳出來、或整份拒收，
沒有人會收到通知。所以這些規則必須有測試守著。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import icsfeed  # noqa: E402
from sources.base import Event, norm_title  # noqa: E402

FAILURES = []

# 假來源的名字。Event.source 要跟它一致，復活機制才對得上
# （「哪個來源抓不到，就撈那個來源的舊活動」）。
SOURCE_NAME = "測試用假來源"


def _future_sample(**overrides) -> "Event":
    """一筆日期在未來的假活動，來源是 SOURCE_NAME。

    復活機制的測試一定要用未來日期 —— 復活會過 is_current()，
    過期的活動本來就該被丟掉。
    """
    from datetime import timedelta as _td

    from sources.base import today_taipei as _today

    fields = {
        "date": (_today() + _td(days=30)).isoformat(),
        "source": SOURCE_NAME,
    }
    fields.update(overrides)
    return sample(**fields)


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


def test_taipei_date_boundary() -> None:
    """過期判準必須釘住**台北**的日期邊界，而且當天的活動不能消失。

    🔴 這條測的是 `base.is_current()` 本身。把 `>=` 打成 `>` 是一字之差，
    症狀卻是「早上還沒上的課，一到當天就從站上不見了」——
    本機隨手跑一次看不出來（要剛好有當天的活動才會顯現），
    也不會有任何錯誤訊息。

    🔴 邊界一律用 `today_taipei()` 算，不是機器日期：workflow 的 runner 跑在
    UTC，排程是台灣 06:00（UTC 前一天 22:00），用機器日期會整整差一天 ——
    每天早上都會把當天的課全部誤判成過期。
    """
    from datetime import timedelta as _timedelta  # noqa: PLC0415

    from sources.base import cutoff_iso, is_current, today_taipei  # noqa: PLC0415

    cutoff = cutoff_iso()

    def dated(offset: int) -> Event:
        return sample(date=(today_taipei() + _timedelta(days=offset)).isoformat())

    check("邊界-當天的活動仍保留", is_current(dated(0), cutoff) is True)
    check("邊界-昨天的活動要丟掉", is_current(dated(-1), cutoff) is False)
    check("邊界-明天的活動保留", is_current(dated(1), cutoff) is True)
    # cutoff 本身必須是台北日期。runner 在 UTC 22:00 跑時，機器日期比台北少一天 ——
    # 若哪天有人把 cutoff_iso() 改成看機器時區，這條會抓到。
    check(
        "邊界-cutoff 是台北的今天（KEEP_PAST_DAYS=0）",
        cutoff == today_taipei().isoformat(),
        "cutoff={} 台北今天={}".format(cutoff, today_taipei().isoformat()),
    )


def test_scrub_contacts() -> None:
    """承辦人的電話／信箱不能跟著地點欄進到公開站上。

    🔴 這幾條的測資一律用**虛構**的號碼與信箱，不要貼來源網站上真實承辦人的
    聯絡方式 —— 測試檔跟著 repo 公開，把真號碼寫進來就是自己把個資推上去
    （這正是這支測試在防的事）。信箱那條還得把字串拆開拼，否則
    scripts/pii-scan.sh 會在自己的測試檔裡抓到信箱樣式而擋下 CI。
    拆開拼不會削弱測試：真正被檢驗的是 scrub_contacts() 執行時的行為。
    """
    from sources.base import scrub_contacts  # noqa: PLC0415

    check("個資-挖市話", scrub_contacts("某某醫院六樓簡報室 洽詢 02-1234-5678 #123") == "某某醫院六樓簡報室 洽詢")
    check("個資-挖信箱", scrub_contacts("報名請洽 nobody" + "@" + "example.invalid") == "報名請洽")
    # 手機規則必須排在市話前面：否則市話規則會從第二個 0 開始咬，
    # 留下一截「09」黏在文字裡 —— 挖一半比沒挖更糟，因為看起來像挖乾淨了
    check("個資-挖手機", scrub_contacts("聯絡 0900-000-000") == "聯絡")
    # 挖完只剩標點就當它是空的，不要留一個「（）」在卡片上
    check("個資-挖完剩標點回空字串", scrub_contacts("( 0912-345-678 )") == "")
    # 正常地址不能被誤傷（門牌號碼、樓層、郵遞區號都有數字）
    address = "臺北市中正區常德街1號 臺大醫院復健大樓"
    check("個資-地址不誤傷", scrub_contacts(address) == address, scrub_contacts(address))
    time_text = "2026/04/18(六) 13:00~18:00"
    check("個資-時間不誤傷", scrub_contacts(time_text) == time_text, scrub_contacts(time_text))


def test_stale_build_keeps_old_events() -> None:
    """一筆都沒抓到的那輪，要把舊活動寫回去**並附上一條告警**。

    🔴 這條守的是「資料默默停止更新、畫面上看不出來」。舊行為是整個檔不覆蓋，
    網站會若無其事地繼續顯示上週的課 —— 使用者沒有任何線索知道那是舊的。
    現在改成沿用舊活動 + errors[0] 放告警 + updated_at 不往前推，
    網站頂端就會跳出提示。
    """
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build  # noqa: E402, PLC0415

    tmpdir = Path(tempfile.mkdtemp())
    original = build.OUTPUT
    original_sources = build.SOURCES
    try:
        build.OUTPUT = tmpdir / "events.json"
        build.OUTPUT.write_text(
            json.dumps(
                {
                    "updated_at": "2026-08-20T06:00:00+08:00",
                    "count": 1,
                    "sources": {SOURCE_NAME: 1},
                    "errors": [],
                    # source 欄位要對得上下面那支假來源的 NAME ——
                    # 復活是照「哪個來源抓不到，就撈那個來源的舊活動」比對的。
                    # 日期必須在**未來**：復活仍然會過 is_current()，
                    # 沿用舊資料是為了頂著，不是為了讓已結束的課復活。
                    # 用 sample() 的預設日期（寫死的 2026-08-22）會讓這筆被濾掉，
                    # 整個檔不會被重寫，測試就會讀到自己種下去的種子而「假通過」。
                    "events": [_future_sample().to_dict()],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        class _Dead:
            NAME = SOURCE_NAME
            __name__ = "dead_source"

            @staticmethod
            def fetch():
                raise RuntimeError("連線逾時")

        build.SOURCES = [_Dead]
        sys.argv = ["build.py"]
        code = build.main()

        check("全源失敗時退出碼是 1", code == 1, "得到 {}".format(code))
        written = json.loads(build.OUTPUT.read_text(encoding="utf-8"))
        check("舊活動有被寫回去（網站不會變 0 筆）", written["count"] == 1, str(written["count"]))
        check(
            "errors 第一條是「這是舊資料」的告警",
            written["errors"] and "舊資料" in written["errors"][0],
            str(written["errors"]),
        )
        check(
            "來源自己的失敗訊息仍在（沒有被告警蓋掉）",
            any("連線逾時" in e for e in written["errors"]),
            str(written["errors"]),
        )
        check(
            "告警指名是哪個來源在用舊資料",
            any(SOURCE_NAME in e and "舊資料" in e for e in written["errors"]),
            str(written["errors"]),
        )
        check(
            "updated_at 沒有往前推（舊資料不能看起來像剛更新）",
            written["updated_at"] == "2026-08-20T06:00:00+08:00",
            written["updated_at"],
        )
        # 訂閱檔一份都不能動：這輪寫回去的是舊資料，重產只會把 DTSTAMP 往前推，
        # 而 events 是空的，照常重產會把七份訂閱檔整批刪掉（訂閱者日曆清空）
        check(
            "沒有產生任何訂閱檔",
            not list(tmpdir.glob("*.ics")),
            str([p.name for p in tmpdir.glob("*.ics")]),
        )
    finally:
        build.OUTPUT = original
        build.SOURCES = original_sources
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_silent_zero_source_is_also_stale() -> None:
    """來源**沒拋例外**、只是靜靜回 0 筆，也要當成故障。

    🔴 這是上面那條測試蓋不到的另一半，而且是更常發生的那一半：
    來源網頁改版時 `soup.select` 選不到列，`fetch()` 不會拋例外也不會 warn()，
    只是回傳一個空 list。若判準寫成「有 error 才算失敗」，這條路徑會被判成
    「成功、今天剛好沒課」——空的 events 往下走，`_write_feeds` 的刪檔分支
    把七份訂閱檔整批刪掉，訂閱者的日曆直接清空且收不到任何提示。
    三個站同時踩到不需要巧合：它們是同一類 ASP 樣板站，會一起改版。
    """
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build  # noqa: E402, PLC0415

    tmpdir = Path(tempfile.mkdtemp())
    original = build.OUTPUT
    original_sources = build.SOURCES
    try:
        build.OUTPUT = tmpdir / "events.json"
        build.OUTPUT.write_text(
            json.dumps(
                {
                    "updated_at": "2026-08-20T06:00:00+08:00",
                    "count": 1,
                    "sources": {SOURCE_NAME: 1},
                    "errors": [],
                    # source 欄位要對得上下面那支假來源的 NAME ——
                    # 復活是照「哪個來源抓不到，就撈那個來源的舊活動」比對的。
                    # 日期必須在**未來**：復活仍然會過 is_current()，
                    # 沿用舊資料是為了頂著，不是為了讓已結束的課復活。
                    # 用 sample() 的預設日期（寫死的 2026-08-22）會讓這筆被濾掉，
                    # 整個檔不會被重寫，測試就會讀到自己種下去的種子而「假通過」。
                    "events": [_future_sample().to_dict()],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # 先放一份訂閱檔，代表「上一輪有產出」——刪檔分支要有東西可刪才測得出來
        (tmpdir / "all.ics").write_bytes(b"PLACEHOLDER")

        class _SilentlyEmpty:
            """改版後選不到任何列的來源：不拋例外、不 warn，就是回空的。"""

            NAME = SOURCE_NAME
            __name__ = "silent_source"

            @staticmethod
            def fetch():
                return []

        build.SOURCES = [_SilentlyEmpty]
        sys.argv = ["build.py"]
        code = build.main()

        check("靜默 0 筆也算失敗（退出碼 1）", code == 1, "得到 {}".format(code))
        written = json.loads(build.OUTPUT.read_text(encoding="utf-8"))
        check("靜默 0 筆時舊活動有寫回去", written["count"] == 1, str(written["count"]))
        check(
            "靜默 0 筆時有「這是舊資料」告警",
            written["errors"] and "舊資料" in written["errors"][0],
            str(written["errors"]),
        )
        # 這條是整段防線的重點：訂閱檔不能被刪掉
        check(
            "靜默 0 筆時既有訂閱檔沒有被刪掉",
            (tmpdir / "all.ics").exists()
            and (tmpdir / "all.ics").read_bytes() == b"PLACEHOLDER",
            str([p.name for p in tmpdir.glob("*.ics")]),
        )
    finally:
        build.OUTPUT = original
        build.SOURCES = original_sources
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_partial_failure_revives_only_dead_source() -> None:
    """一個來源掛掉時，只有**它**的活動沿用舊資料，活著的來源照常用新的。

    🔴 這是 2026-08-26 06:20 真實事故的形狀：主來源 522、次來源連線逾時，
    只有基金會活著 —— 站上從 58 筆掉到 4 筆，等於整個站空掉，
    而畫面上只有兩行「抓取失敗」的小字，看不出「少了 54 場課」。
    掛掉的來源要用舊資料頂著，活著的來源不能被牽連（那會讓新公告的課消失）。
    """
    import shutil  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build  # noqa: E402, PLC0415

    tmpdir = Path(tempfile.mkdtemp())
    original = build.OUTPUT
    original_sources = build.SOURCES
    alive_name = "測試用活著的來源"
    try:
        build.OUTPUT = tmpdir / "events.json"
        build.OUTPUT.write_text(
            json.dumps(
                {
                    "updated_at": "2026-08-20T06:00:00+08:00",
                    "count": 2,
                    "sources": {SOURCE_NAME: 1, alive_name: 1},
                    "errors": [],
                    "events": [
                        _future_sample(title="掛掉那個來源的舊課").to_dict(),
                        # 活著的來源在舊檔裡也有一筆。它**不能**被復活 ——
                        # 那筆課若已經下架／取消，復活會讓它永遠留在站上。
                        _future_sample(
                            title="活著來源的舊課", source=alive_name
                        ).to_dict(),
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        class _Dead:
            NAME = SOURCE_NAME
            __name__ = "dead_source"

            @staticmethod
            def fetch():
                raise RuntimeError("522")

        class _Alive:
            NAME = alive_name
            __name__ = "alive_source"

            @staticmethod
            def fetch():
                return [_future_sample(title="活著來源的新課", source=alive_name)]

        build.SOURCES = [_Dead, _Alive]
        sys.argv = ["build.py"]
        code = build.main()

        # 還有來源活著、也真的抓到東西 → 這輪不算整體失敗
        check("部分失敗時退出碼是 0", code == 0, "得到 {}".format(code))
        written = json.loads(build.OUTPUT.read_text(encoding="utf-8"))
        titles = sorted(e["title"] for e in written["events"])
        check(
            "掛掉來源的舊課有頂上",
            "掛掉那個來源的舊課" in titles,
            str(titles),
        )
        check("活著來源的新課有進來", "活著來源的新課" in titles, str(titles))
        check(
            "活著來源的舊課沒有被復活（已下架的課不該留在站上）",
            "活著來源的舊課" not in titles,
            str(titles),
        )
        check(
            "告警指名是哪個來源在用舊資料",
            any(SOURCE_NAME in e and "舊資料" in e for e in written["errors"]),
            str(written["errors"]),
        )
        check(
            "沒有把活著的來源也講成舊資料",
            not any(alive_name in e and "舊資料" in e for e in written["errors"]),
            str(written["errors"]),
        )
        check(
            "有來源沿用舊資料時 updated_at 不往前推",
            written["updated_at"] == "2026-08-20T06:00:00+08:00",
            written["updated_at"],
        )

        # 反向：來源掛了，但舊檔裡也沒有它的活動 → 畫面上 100% 是新資料，
        # 這時把 updated_at 往回釘就是**反方向的謊**（新資料被標成舊時間）。
        # 判準必須是「真的有舊資料被併進來」，不是「有沒有來源掛掉」。
        build.OUTPUT.write_text(
            json.dumps(
                {
                    "updated_at": "2026-08-20T06:00:00+08:00",
                    "count": 1,
                    "sources": {alive_name: 1},
                    "errors": [],
                    # 舊檔裡**沒有**掛掉那個來源的任何活動
                    "events": [
                        _future_sample(title="活著來源的舊課", source=alive_name).to_dict()
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        build.main()
        written = json.loads(build.OUTPUT.read_text(encoding="utf-8"))
        check(
            "沒有舊資料可沿用時 updated_at 照常往前推",
            written["updated_at"] != "2026-08-20T06:00:00+08:00",
            written["updated_at"],
        )
        check(
            "此時仍要講明那個來源這次沒抓到",
            any(SOURCE_NAME in e for e in written["errors"]),
            str(written["errors"]),
        )
        # 同一個判準也管訂閱檔：沒有東西被復活＝訂閱檔本來就沒有那個來源的內容，
        # 照常重產不會讓任何人的日曆少東西，反而讓活著來源的新課當天就進得去。
        check(
            "沒有舊資料可沿用時訂閱檔照常重產",
            (tmpdir / "all.ics").exists(),
            str([p.name for p in tmpdir.glob("*.ics")]),
        )
    finally:
        build.OUTPUT = original
        build.SOURCES = original_sources
        shutil.rmtree(tmpdir, ignore_errors=True)


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
    test_taipei_date_boundary()
    test_scrub_contacts()
    test_stale_build_keeps_old_events()
    test_silent_zero_source_is_also_stale()
    test_partial_failure_revives_only_dead_source()

    if FAILURES:
        print("\n{} 項未通過：{}".format(len(FAILURES), "、".join(FAILURES)), file=sys.stderr)
        return 1
    print("\n全部通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
