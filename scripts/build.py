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
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources import lien, pmr, tapedpmr  # noqa: E402
from sources.base import TAIPEI, Event, cutoff_iso, drain_warnings  # noqa: E402

SOURCES = [pmr, tapedpmr, lien]

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "data" / "events.json"

def _norm_title(title: str) -> str:
    """去掉積分/時數註記與所有空白標點，用來判斷兩筆是不是同一場活動。"""
    text = re.sub(r"[(（][^)）]*[)）]", "", title)
    return re.sub(r"[\s\-—－_、,，.。:：;；]", "", text).lower()


def dedupe(events: List[Event]) -> List[Event]:
    """同一天 + 標題實質相同 = 同一場。保留欄位比較齊的那筆。"""
    best: Dict[tuple, Event] = {}
    for event in events:
        key = (event.date, _norm_title(event.title))
        current = best.get(key)
        if current is None or _completeness(event) > _completeness(current):
            best[key] = event
    return sorted(best.values(), key=lambda e: (e.date, e.title))


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
    tmp = OUTPUT.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(OUTPUT)  # 原子寫入：中途失敗不會留下半截 JSON
    print("已寫入 {}".format(OUTPUT))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
