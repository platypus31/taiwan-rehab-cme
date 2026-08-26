"""共用資料結構與解析工具。

每個來源（sources/*.py）只負責「把該學會的網頁變成 Event 清單」，
其餘工作（地區判定、積分解析、去重、輸出 JSON）全部集中在這裡，
所以之後要加新來源，只要寫一支新的 fetch() 就好。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 30


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------
@dataclass
class Event:
    """一筆繼續教育活動。所有來源都必須產出這個形狀。"""

    date: str  # ISO 格式 YYYY-MM-DD
    title: str
    organizer: str = ""
    location: str = ""
    credits: Optional[float] = None  # 復健醫學積分（點）
    region: str = "其他"  # 由 location 推導
    source: str = ""  # 學會名稱
    url: str = ""
    categories: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# 這個站的「今天」一律是台灣的今天。
#
# 不能用機器的本地時間：GitHub Actions 的 runner 跑在 UTC，排程是台灣 06:00
# （UTC 前一天 22:00），用 runner 的日期會整整差一天，把當天的活動誤判成過期。
# 台灣沒有日光節約時間，固定 +08:00 就夠，不需要 tzdata。
TAIPEI = timezone(timedelta(hours=8))

# 保留過去幾天的活動。2026-08-17 定案：過期的課程不要出現在站上，所以是 0。
# 定義在這裡而不是 build.py，是為了讓「來源層自己過濾」與「彙整層過濾」用同一個下界。
KEEP_PAST_DAYS = 0


class SourceError(Exception):
    """單一來源抓取失敗。build.py 會記錄並繼續跑其他來源。"""


# 「有抓到東西，但不完整」的情況（例如頁數超過安全上限被截斷）。
# 這種事最怕默默發生 —— 資料看起來正常、只是變少，沒人會發現。
# 所以一律收集起來交給 build.py 寫進 events.json，讓網站頂端顯示出來。
_WARNINGS: List[str] = []


def warn(message: str) -> None:
    _WARNINGS.append(message)


def drain_warnings() -> List[str]:
    """取出並清空目前累積的警告（build.py 每跑完一個來源呼叫一次）。"""
    global _WARNINGS
    collected, _WARNINGS = _WARNINGS, []
    return collected


def get(url: str, **kwargs) -> requests.Response:
    """帶 UA 與 timeout 的 GET，失敗轉成 SourceError。"""
    try:
        resp = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, **kwargs
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SourceError("GET {} 失敗: {}".format(url, exc)) from exc
    return resp


# --------------------------------------------------------------------------
# 積分解析
# --------------------------------------------------------------------------
# 標題常見寫法：「...(共3時)(1.5點)」「...(2點)」「...(積分1.5)」
#
# 第三條是給多梯次課程用的 fallback（例如「(3/3點)」「(2.2/1.5點)」，前兩條吃不到），
# 但它必須限制在括號內、且「點」後面不能接時間用語 —— 否則「9點報到」「9點30分開始」
# 這種活動時間標示會被當成 9 點積分（已實測會誤判，不是理論風險）。
_CREDIT_PATTERNS = [
    re.compile(r"[(（]\s*(\d+(?:\.\d+)?)\s*點\s*[)）]"),
    re.compile(r"積分\s*[:：]?\s*(\d+(?:\.\d+)?)"),
    re.compile(
        r"[(（][^)）]{0,40}?(\d+(?:\.\d+)?)\s*點"
        r"(?!\s*(?:\d|半|報到|開始|整|鐘|至|到|前|後|以|準))[^)）]{0,15}?[)）]"
    ),
]


def parse_credits(text: str) -> Optional[float]:
    """從標題或說明文字抽出積分點數；抽不到回 None（前端會歸類為「未標示」）。"""
    if not text:
        return None
    for pattern in _CREDIT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                value = float(match.group(1))
            except ValueError:
                continue
            # 單場繼續教育積分極少超過 20 點，超出視為誤抓（例如把年份吃進來）
            if 0 < value <= 20:
                return value
    return None


# --------------------------------------------------------------------------
# 地區判定
# --------------------------------------------------------------------------
_ONLINE_KEYWORDS = [
    "線上",
    "視訊",
    "遠距",
    "webex",
    "zoom",
    "google meet",
    "meet.google",
    "teams",
    "youtube",
    "直播",
    "雲端",
]

_REGION_MAP = [
    ("北部", ["台北", "臺北", "新北", "基隆", "桃園", "新竹", "宜蘭"]),
    ("中部", ["台中", "臺中", "苗栗", "彰化", "南投", "雲林", "竹山", "斗六", "員林"]),
    ("南部", ["高雄", "台南", "臺南", "嘉義", "屏東", "澎湖"]),
    ("東部", ["花蓮", "台東", "臺東"]),
    ("離島", ["金門", "馬祖", "連江", "綠島", "蘭嶼"]),
]

# 地點只寫「某某醫院會議室」沒寫縣市時的補救對應。
# 刻意只收「單一院區、不會認錯」的機構 —— 長庚／榮總／馬偕這種多院區的一律不放，
# 寧可落在「其他」也不要標錯地區（標錯比沒標更糟，使用者會白跑一趟）。
_INSTITUTION_MAP = [
    ("北部", ["台大醫院", "臺大醫院", "三軍總醫院", "振興醫", "和信治癌", "萬芳醫"]),
    ("中部", ["中國醫藥大學", "中山醫學", "若瑟醫"]),
    ("南部", ["成大醫", "奇美醫", "安泰醫"]),
]


# 地區 → 檔名用的 ASCII 代號。訂閱檔（data/region-*.ics）的檔名要能放進網址，
# 中文檔名經過各家日曆 App 的 percent-encoding 會變成很難除錯的東西。
#
# 🔴 **這份對應表是唯一真實來源**：build.py 用它決定產哪些檔，
# 並把「地區 → 檔名」整份寫進 events.json 的 `feeds` 欄位交給前端。
# 前端**不要**自己再抄一份 —— 抄一份的話，這裡加了新地區而前端沒跟上，
# 使用者會拿到 404 的訂閱網址，而且沒有任何錯誤訊息。
# 沒列在這裡的地區不會有自己的訂閱檔（仍然收在「全部」那份裡）。
REGION_SLUGS = {
    "北部": "north",
    "中部": "central",
    "南部": "south",
    "東部": "east",
    "離島": "islands",
    "線上": "online",
    "其他": "other",
}


def detect_region(location: str, organizer: str = "") -> str:
    """由地點判斷地區。線上優先（線上課程沒有地理限制，是獨立篩選軸）。

    刻意不看標題：標題裡的「視訊」「遠距」多半是課程**主題**而非上課形式
    （實例：「偏鄉視訊語言治療經驗」是實體課，講的是視訊治療），
    把標題餵進來會把實體課誤判成線上課。
    地點沒寫縣市時（例如只寫「六樓簡報室」）才退而用主辦單位推斷。
    """
    place = (location or "").lower()
    for keyword in _ONLINE_KEYWORDS:
        if keyword in place:
            return "線上"
    for blob in (place, (organizer or "").lower()):
        for region, keywords in _REGION_MAP:
            for keyword in keywords:
                if keyword in blob:
                    return region
    for blob in (place, (organizer or "").lower()):
        for region, keywords in _INSTITUTION_MAP:
            for keyword in keywords:
                if keyword.lower() in blob:
                    return region
    return "其他"


# --------------------------------------------------------------------------
# 分類判定
# --------------------------------------------------------------------------
# 復健科次專科軸。原站（神經科）用的是神經次專科，這裡換成復健科的實際分工。
_CATEGORY_MAP = [
    ("神經復健", ["中風", "腦中風", "腦傷", "脊髓", "神經復健", "巴金森", "帕金森", "失智"]),
    ("骨骼肌肉", ["骨科", "肌肉骨骼", "關節", "肩", "膝", "脊椎", "下背痛", "骨鬆", "骨質疏鬆"]),
    ("兒童復健", ["兒童", "早療", "早期療育", "發展遲緩", "腦性麻痺", "學齡", "青少年", "adhd"]),
    ("心肺復健", ["心肺", "心臟", "肺", "呼吸", "肺復原", "心衰"]),
    ("吞嚥與語言", ["吞嚥", "語言", "構音", "失語", "咀嚼"]),
    ("運動醫學", ["運動傷害", "運動處方", "運動醫學", "體適能", "棒球", "跑者"]),
    # 「介入」兩字刻意不放：復健領域「早期療育介入」「臨床介入」用得太浮濫，
    # 放進來會把一堆跟針具無關的課誤標成介入性治療（實測誤收 3/9 筆）
    ("超音波與介入", ["超音波", "注射", "增生療法", "神經阻斷", "震波", "導引下"]),
    ("輔具與義肢", ["輔具", "義肢", "支架", "矯具", "輪椅"]),
    ("疼痛", ["疼痛", "痛症", "肌筋膜"]),
    ("長照與老年", ["長照", "老年", "衰弱", "失能", "居家", "社區"]),
]


def detect_categories(title: str, extra: str = "") -> List[str]:
    """依關鍵字標記分類，可多標。全都不中就不標（前端顯示為「其他」）。"""
    blob = "{} {}".format(title or "", extra or "").lower()
    hits = []
    for category, keywords in _CATEGORY_MAP:
        for keyword in keywords:
            if keyword.lower() in blob:
                hits.append(category)
                break
    return hits


# --------------------------------------------------------------------------
# 日期解析
# --------------------------------------------------------------------------
# (?<!\d) 是必要的數字邊界：沒有它，民國年那條會從「2026.08.22」的後三碼
# 抓出「026」再 +1911 變成 1937 年（Codex review 抓到，已實測會發生）。
_DATE_PATTERNS = [
    re.compile(r"(?<!\d)(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})"),
    # 民國年。分隔符含「年/月」，因為學會網站常寫成「115年08月22日」或「115.08.22」
    re.compile(r"(?<!\d)(\d{3})[/\-.年](\d{1,2})[/\-.月](\d{1,2})"),
]


def parse_date(text: str) -> Optional[str]:
    """解析日期成 ISO 字串。支援西元與民國年（3 位數自動 +1911）。"""
    if not text:
        return None
    cleaned = unicodedata.normalize("NFKC", text)
    for pattern in _DATE_PATTERNS:
        match = pattern.search(cleaned)
        if not match:
            continue
        year, month, day = (int(g) for g in match.groups())
        if year < 1911:  # 民國年
            year += 1911
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


def norm_title(title: str) -> str:
    """去掉積分/時數註記與所有空白標點，用來判斷兩筆是不是同一場活動。

    🔴 **這支有兩個消費者，而且它們必須永遠一致**：
      ・`scripts/build.py` 的 dedupe key —— 決定「兩筆是不是同一場」
      ・`sources/icsfeed.py` 的 UID —— 決定「訂閱端會不會把它當成新事件」
    兩邊若各寫一份，哪天有人只改了其中一份，訂閱端就會無聲地重複跳出同一場活動
    （而且測試不會紅）。所以放在 base 讓兩邊共用，**不要再複製第三份出去**。

    ⚠️ 改動這裡的正規化規則會讓既有訂閱者的事件全部重新產生一次
    （UID 跟著變），非必要不要動。
    """
    text = re.sub(r"[(（][^)）]*[)）]", "", title or "")
    return re.sub(r"[\s\-—－_、,，.。:：;；]", "", text).lower()


def clean_text(text: str) -> str:
    """壓掉多餘空白與全形空格。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.replace("　", " ")).strip()


def strip_prefix(text: str, *prefixes: str) -> str:
    """去掉「主辦：」「地點：」這類前綴。"""
    result = clean_text(text)
    for prefix in prefixes:
        for sep in ("：", ":"):
            token = prefix + sep
            if result.startswith(token):
                result = result[len(token) :].strip()
    return result


def today_taipei() -> date:
    """台灣的今天。所有日期比較都從這裡出發，不看機器時區。"""
    return datetime.now(TAIPEI).date()


def today_iso() -> str:
    return today_taipei().isoformat()


def cutoff_iso() -> str:
    """資料保留下界：早於這天的活動一律不收。

    來源層若要自己過濾過期活動，必須用這個函式而不是 today_iso()，
    否則 build.py 的 KEEP_PAST_DAYS 對該來源會失效 ——
    資料看起來正常，只是跟彙整層對不上。
    """
    return (today_taipei() - timedelta(days=KEEP_PAST_DAYS)).isoformat()


def is_current(event: "Event", cutoff: str) -> bool:
    """這筆活動還該不該留在站上。

    🔴 邊界：**活動當天仍算未結束**（比較用 >= 不是 >）。
    早上還沒上的課，不能因為「今天」就從站上消失。

    日期一律是台北的日期（cutoff 由 cutoff_iso() 產出，走 today_taipei()）——
    workflow 的 runner 跑在 UTC，用機器日期會整整差一天。

    這件事本身只有一行，抽成函式是為了**讓它測得到**：
    原本這條判準寫在 build.py 的 list comprehension 裡，而 scripts/ 沒有
    `__init__.py`，selftest 的 sys.path 技巧 import 不到 scripts.build，
    等於整條過期判準沒有任何測試守著 —— 把 `>=` 打成 `>` 這種一字之差，
    症狀是「當天的課早上就消失了」，本機跑不出來、也不會有錯誤訊息。
    """
    return event.date >= cutoff


# --------------------------------------------------------------------------
# 個資防線
# --------------------------------------------------------------------------
# 學會官網的活動頁常在地點或主辦欄旁邊接著寫承辦人的信箱與分機
# （欄位不是獨立的，是同一段自由文字，躲不掉）。那些是個資，
# 公開站沒有理由轉載，所以凡是從官網自由文字抽出來的欄位都要先過這一關
# 再放進 Event。
#
# 這是 scripts/pii-scan.sh 的**上游**，不是替代品：掃描器是最後一道閘門，
# 這裡則是讓資料一開始就不髒。兩道都要有 —— 只靠掃描器的話，
# 每天自動更新會在某天突然變紅，而且那時髒資料已經在 events.json 裡了。
#
# 🔴 兩條電話規則都要用 (?<!\d) 與 (?!\d) 綁數字邊界，而且**手機那條要排在市話前面**：
# 沒有邊界的話，市話規則會從「0900-000-000」的第二個 0 開始比對、只咬掉後半段，
# 留下一截「09」黏在文字裡 —— 挖一半比沒挖更糟，因為看起來像挖乾淨了。
_CONTACT_PATTERNS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),  # 信箱
    re.compile(r"(?<!\d)09[0-9]{2}[-\s]?[0-9]{3}[-\s]?[0-9]{3}(?!\d)"),  # 手機
    re.compile(
        r"(?<!\d)0[0-9]{1,2}[-\s]?[0-9]{3,4}[-\s]?[0-9]{3,4}"
        r"(?:\s*[#分機]+\s*[0-9]+)?(?!\d)"
    ),
]


def scrub_contacts(text: str) -> str:
    """把信箱與電話從自由文字裡挖掉。挖完只剩標點就回空字串。"""
    result = text or ""
    for pattern in _CONTACT_PATTERNS:
        result = pattern.sub("", result)
    result = clean_text(result)
    # 挖掉之後可能剩下「聯絡人：」「（）」這種殘骸，沒有中英數就當它是空的
    if not re.search(r"[0-9A-Za-z一-鿿]", result):
        return ""
    return result
