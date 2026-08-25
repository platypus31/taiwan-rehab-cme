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


def _write_ics(target: Path, text: str) -> None:
    """原子寫出一份 .ics。

    newline="" 是必要的：ics 規範要求 CRLF 換行，`icsfeed.render()` 已經產好 \\r\\n，
    用預設模式寫檔會被再翻譯一次變成 \\r\\r\\n（`Path.write_text` 在 3.9 沒有
    newline 參數，所以這裡用 open）。
    """
    tmp = target.with_suffix(".ics.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    tmp.replace(target)


def _write_feeds(events: List[Event], updated_at: str) -> Dict[str, str]:
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
    """
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
        _write_ics(all_target, icsfeed.render(events, base_name, dtstamp=stamp))
        feeds[""] = ALL_FEED  # 空字串＝沒選地區（全部）
        print("已寫入 {}（{} 筆）".format(all_target, len(events)))
    elif all_target.exists():
        all_target.unlink()
        print("已移除 {}（這次沒有活動）".format(all_target))

    for region, slug in REGION_SLUGS.items():
        rows = [e for e in events if e.region == region]
        target = data_dir / "region-{}.ics".format(slug)
        if not rows:
            if target.exists():
                target.unlink()
                print("已移除 {}（這次沒有活動）".format(target))
            continue
        name = "{}（{}）".format(base_name, region)
        _write_ics(target, icsfeed.render(rows, name, dtstamp=stamp))
        feeds[region] = target.name
        print("已寫入 {}（{} 筆）".format(target, len(rows)))

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

    for module in SOURCES:
        name = getattr(module, "NAME", module.__name__)
        try:
            fetched = module.fetch()
        except Exception as exc:  # noqa: BLE001 - 單源失敗不中斷全體
            errors.append("{}：{}".format(name, exc))
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
    payload["feeds"] = _write_feeds(events, payload["updated_at"])

    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(OUTPUT)  # 原子寫入：中途失敗不會留下半截 JSON
    print("已寫入 {}".format(OUTPUT))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
