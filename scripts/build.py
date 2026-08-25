#!/usr/bin/env python3
"""跑所有來源 → 合併去重 → 寫出 data/events.json。

要加新來源：在 sources/ 底下寫一支有 fetch() 與 NAME 的模組，
然後把它加進下面的 SOURCES 清單。其他什麼都不用改。

單一來源掛掉不會讓整包失敗 —— 會記在輸出 JSON 的 errors 欄位裡，
前端會把它顯示出來，這樣來源網站改版時看得見，而不是資料默默變少。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import icsfeed, lien, pmr, tapedpmr  # noqa: E402
from sources.base import (  # noqa: E402
    REGION_SLUGS,
    TAIPEI,
    Event,
    cutoff_iso,
    drain_warnings,
    norm_title,
)

SOURCES = [pmr, tapedpmr, lien]

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "events.json"

# 「全部」那份訂閱檔的檔名。前端把它當成沒有選地區時的退路，
# 也是遇到 REGION_SLUGS 沒收錄的地區時的退路。
ALL_FEED = "all.ics"


def dedupe(events: List[Event]) -> List[Event]:
    """同一天 + 標題實質相同 = 同一場。保留欄位比較齊的那筆。"""
    best: Dict[tuple, Event] = {}
    for event in events:
        key = (event.date, norm_title(event.title))
        current = best.get(key)
        if current is None or _completeness(event) > _completeness(current):
            best[key] = event
    return sorted(best.values(), key=lambda e: (e.date, e.title))


def _without_dtstamp(text: str) -> str:
    """把 DTSTAMP 行拿掉，用來比較「除了時間戳以外有沒有真的變」。

    DTSTAMP 短到不可能被折行（`DTSTAMP:20260825T223402Z` 才 24 個字元，
    上限是 75），所以逐行 startswith 是安全的。
    """
    return "\r\n".join(
        line for line in text.split("\r\n") if not line.startswith("DTSTAMP:")
    )


def _write_ics(target: Path, text: str) -> bool:
    """原子寫出一份 .ics；內容實質沒變就不動它。回傳有沒有真的寫。

    🔴 **DTSTAMP 以外的內容一樣就不重寫**，這件事是必要的而不是最佳化：
    DTSTAMP 取自 build 的 `updated_at`，而那是**每次 build 的當下時間**，
    所以每天跑一定不一樣。若照寫，七份訂閱檔會在活動一場都沒變的日子
    照樣天天產生 diff —— git 歷史被無意義的 commit 洗版，
    而且沒有人分得出「今天真的有新課」還是「只是時間戳跳動」。
    保留舊 DTSTAMP 在語義上也更正確：RFC 5545 的 DTSTAMP 是「這筆資訊最後
    被修訂的時間」，資料沒被修訂就不該往前跳。

    newline="" 是必要的：ics 規範要求 CRLF 換行，`icsfeed.render()` 已經產好 \\r\\n，
    用預設模式寫檔會被再翻譯一次變成 \\r\\r\\n（`Path.write_text` 在 3.9 沒有
    newline 參數，所以這裡用 open）。讀回來比較時同樣要 newline=""，
    否則 universal newline 會把 \\r\\n 換成 \\n，比出來永遠不相等。
    """
    # 讀舊檔是這支唯一會碰到「外部世界」的地方：檔案可能被手動改壞、
    # 編碼壞掉、或上次寫到一半被中斷。那種情況下**照常重寫**就好 ——
    # 比對只是為了少產生 diff，絕不能因為比對失敗就讓整個 build 掛掉。
    if target.exists():
        try:
            with open(target, encoding="utf-8", newline="") as handle:
                if _without_dtstamp(handle.read()) == _without_dtstamp(text):
                    return False
        except (UnicodeDecodeError, OSError) as exc:
            print(
                "[warn] 讀不到或讀不懂既有的 {}（{}），直接重寫".format(target.name, exc),
                file=sys.stderr,
            )

    tmp = target.with_suffix(".ics.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    tmp.replace(target)
    return True


def _region_feed_name(slug: str) -> str:
    """地區訂閱檔的檔名規則。**只有這一個地方定義它。**

    `_write_feeds()`（產檔／刪檔）與 `_feeds_on_disk()`（來源掛掉時回報現況）
    都要用同一份規則。各寫一份的話，哪天改了命名只改到一處，
    events.json 的 feeds 就會指向不存在的檔案 —— 使用者按訂閱拿到 404，
    而且不會有任何錯誤訊息。
    """
    return "region-{}.ics".format(slug)


def _feeds_on_disk() -> Dict[str, str]:
    """照目前磁碟上實際存在的檔案回報 feeds 對照表。

    來源掛掉、這輪不重產訂閱檔時用它，讓 events.json 的 feeds 欄位仍然
    跟真實檔案一致 —— 指向不存在的檔案會讓使用者拿到 404。
    """
    data_dir = OUTPUT.parent
    feeds: Dict[str, str] = {}
    if (data_dir / ALL_FEED).exists():
        feeds[""] = ALL_FEED
    for region, slug in REGION_SLUGS.items():
        name = _region_feed_name(slug)
        if (data_dir / name).exists():
            feeds[region] = name
    return feeds


def _write_feeds(events: List[Event], updated_at: str, degraded: bool = False) -> Dict[str, str]:
    """產出 .ics 訂閱檔，回傳「地區 → 檔名」給前端用。

    訂閱刻意做成**選配**：值班不一定每堂課都上得到，上不到的課進了日曆就只是雜訊，
    所以要不要進日曆由使用者自己按，而且要能只訂自己去得成的那些。

    🔴 **粒度刻意選「地區」而不是主題**：地區是唯一「一筆活動恰好落在一個值」的軸，
    所以同一場活動不會同時出現在兩份訂閱檔裡。主題（categories）是可複標的，
    一場活動可能同時是「兒童復健」與「吞嚥與語言」，使用者訂了兩份就會在日曆上
    看到同一場活動兩次（UID 相同但分屬不同行事曆，日曆 App 不會幫你合併）。
    地區同時也是真正決定「去不去得成」的條件 —— 人在北部就是到不了台東。

    ⚠️ 沒有活動的地區**不產檔**：一份沒有任何 VEVENT 的行事曆，
    在訂閱端會顯示成壞掉的行事曆而不是「這個地區目前沒有課」。
    對應地，上一次 build 有、這次沒有的地區檔要**刪掉** —— 留著的話，
    已經訂閱的人會永遠收到那份不再更新的舊資料，而且完全沒有徵兆。

    🔴 **有來源抓失敗時整批不重產（`degraded`）。**
    網站跟訂閱檔是兩種不同的媒介，安全標準不一樣：
    events.json 少了一半資料時，網站頂端會跳一條「部分來源這次沒抓到資料」的提示，
    使用者看得到、知道要去官網確認；但 **.ics 沒有任何地方可以放那條提示** ——
    訂閱者的日曆只會安靜地少掉一半活動，甚至整份行事曆消失（該地區這輪 0 筆→檔案被刪→404），
    而且不會收到任何通知。
    實例（2026-08-26 06:20 的自動更新）：三個來源掛了兩個（522 與連線逾時），
    當次 events.json 從 60 筆掉到 4 筆。那時若照常重產訂閱檔，
    七份會變成「all 剩 4 筆 + 五個地區檔直接被刪」。
    所以只要**有來源是整個抓失敗**，這輪就完全不動訂閱檔：寧可訂閱者收到的是舊資料，
    也不要讓他們的行事曆被清空。網站那邊照舊顯示當次結果與錯誤提示，不受影響。

    ⚠️ 代價是「來源永久壞掉時訂閱檔會一直停在舊資料」。所以這件事必須**吵**：
    stderr 會印警告，而且網站上的錯誤提示也會天天出現，不會沒人發現。
    """
    if degraded:
        print(
            "[warn] 這輪有來源抓取失敗，訂閱檔一律不重產也不刪除（保留上一次的內容）。"
            "訂閱端沒有地方顯示錯誤，寧可舊也不要把別人的行事曆清空。",
            file=sys.stderr,
        )
        return _feeds_on_disk()

    stamp = icsfeed.utc_stamp(updated_at)
    data_dir = OUTPUT.parent
    base_name = "復健醫學 繼續教育活動"
    feeds: Dict[str, str] = {}

    # 「全部」那份也適用「空的不產檔」規則 —— 三個來源同時清空並非不可能
    # （改版、季節性沒有課），那時寫出一份零 VEVENT 的行事曆，
    # 訂閱端顯示的是「壞掉的行事曆」而不是「目前沒有課」。
    # feeds 少了 "" 這個鍵，前端就會把整個訂閱區塊藏起來，這正是我們要的。
    all_target = data_dir / ALL_FEED
    if events:
        wrote = _write_ics(all_target, icsfeed.render(events, base_name, dtstamp=stamp))
        feeds[""] = ALL_FEED  # 空字串＝沒選地區（全部）
        print("{} {}（{} 筆）".format("已寫入" if wrote else "無變動，保留", all_target, len(events)))
    elif all_target.exists():
        all_target.unlink()
        print("已移除 {}（這次沒有活動）".format(all_target))

    for region, slug in REGION_SLUGS.items():
        rows = [e for e in events if e.region == region]
        target = data_dir / _region_feed_name(slug)
        if not rows:
            if target.exists():
                target.unlink()
                print("已移除 {}（這次沒有活動）".format(target))
            continue
        name = "{}（{}）".format(base_name, region)
        wrote = _write_ics(target, icsfeed.render(rows, name, dtstamp=stamp))
        feeds[region] = target.name
        print("{} {}（{} 筆）".format("已寫入" if wrote else "無變動，保留", target, len(rows)))

    # 地區在資料裡但沒收進 REGION_SLUGS：不會有自己的訂閱檔（仍在「全部」那份裡），
    # 前端會退回「全部」。浮出來讓人看得見，不要默默發生。
    missing = sorted({e.region for e in events} - set(REGION_SLUGS))
    if missing:
        print(
            "[warn] 這些地區沒有對應的檔名代號，不會有專屬訂閱檔：{}"
            "（請補進 sources/base.py 的 REGION_SLUGS）".format("、".join(missing)),
            file=sys.stderr,
        )
    return feeds


def _completeness(event: Event) -> int:
    score = 0
    for value in (event.location, event.organizer, event.url):
        if value:
            score += 1
    if event.credits is not None:
        score += 1
    if event.region != "其他":
        score += 1
    return score


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true", help="只印統計，不寫檔"
    )
    args = parser.parse_args()

    # 台灣的今天（見 sources/base.py 的 TAIPEI 註解 —— runner 跑在 UTC，不能用機器日期）
    cutoff = cutoff_iso()

    collected: List[Event] = []
    errors: List[str] = []
    per_source: Dict[str, int] = {}
    # 「整個來源抓失敗」的清單。刻意跟 errors 分開記 —— errors 裡也會有
    # 「某一筆的詳情頁沒抓到」這種局部警告，那種不該讓訂閱檔整批凍結。
    failed_sources: List[str] = []

    for module in SOURCES:
        name = getattr(module, "NAME", module.__name__)
        try:
            fetched = module.fetch()
        except Exception as exc:  # noqa: BLE001 - 單源失敗不中斷全體
            errors.append("{}：{}".format(name, exc))
            failed_sources.append(name)
            per_source[name] = 0
            print("[FAIL] {} — {}".format(name, exc), file=sys.stderr)
            continue
        fresh = [e for e in fetched if e.date >= cutoff]
        collected.extend(fresh)
        per_source[name] = len(fresh)
        print("[ok] {} — {} 筆（原始 {}）".format(name, len(fresh), len(fetched)))

        # 抓到了但不完整（例如頁數被截斷）也要浮出來，不能只有整個掛掉才報
        for message in drain_warnings():
            errors.append(message)
            print("[warn] {}".format(message), file=sys.stderr)

    events = dedupe(collected)

    payload = {
        "updated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "count": len(events),
        "sources": per_source,
        "errors": errors,
        "events": [e.to_dict() for e in events],
    }

    print(
        "合計 {} 筆（去重前 {}），來源 {} 個，錯誤 {} 個".format(
            len(events), len(collected), len(per_source), len(errors)
        )
    )

    # 全部來源都掛掉才算失敗（部分失敗仍要保留可用資料）。
    # dry-run 也要套同一套判斷，否則拿它當健康檢查會永遠得到成功碼。
    exit_code = 1 if errors and not events else 0

    if args.dry_run:
        return exit_code

    # 全部來源都掛掉時保留既有 events.json —— 寧可資料舊，也不要把網站洗成 0 筆。
    # （兩個來源同時短暫連不上並不罕見：對方網站維護、本機斷網都會這樣。）
    if exit_code == 1:
        print(
            "全部來源失敗，保留既有 {} 不覆蓋".format(OUTPUT), file=sys.stderr
        )
        return exit_code

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    # 訂閱檔先寫，events.json 後寫：`feeds` 這份對照表要進 payload 給前端，
    # 而且順序這樣排的話，前端讀到新的 feeds 時檔案一定已經在了。
    payload["feeds"] = _write_feeds(
        events, payload["updated_at"], degraded=bool(failed_sources)
    )

    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(OUTPUT)  # 原子寫入：中途失敗不會留下半截 JSON
    print("已寫入 {}".format(OUTPUT))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
