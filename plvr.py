"""內政部不動產成交案件實價登錄開放資料。

資料來源：https://plvr.land.moi.gov.tw/DownloadOpenData
相較於 591 / 樂居，這份官方資料的門牌未遮蔽，且「車位總價元」「車位移轉總面積」
是登錄時就分開申報的欄位，不需要自己從總價反推車位價。
"""

import csv
import io
import re
import ssl
import statistics
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

CACHE_DIR = Path(__file__).with_name("plvr_cache")
SQM_PER_PING = 3.305785
DOWNLOAD_URL = "https://plvr.land.moi.gov.tw/DownloadSeason?season={season}&fileName=lvr_landcsv.zip"

CITY_FILE_CODES = {
    "台北市": "a", "台中市": "b", "基隆市": "c", "台南市": "d", "高雄市": "e",
    "新北市": "f", "宜蘭縣": "g", "桃園市": "h", "嘉義市": "i", "新竹縣": "j",
    "苗栗縣": "k", "南投縣": "m", "彰化縣": "n", "新竹市": "o", "雲林縣": "p",
    "嘉義縣": "q", "屏東縣": "t", "花蓮縣": "u", "台東縣": "v", "金門縣": "w",
    "澎湖縣": "x", "連江縣": "z",
}

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_CN_NUMERALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass
class LandDeal:
    date: str
    address: str
    floor: str
    floor_no: Optional[int]
    total_floor: Optional[int]
    total_wan: float
    parking_wan: float
    total_ping: float
    parking_ping: float
    house_ping: float
    house_unit_price: Optional[float]
    parking_type: str
    layout: str
    building_type: str
    build_year: Optional[int]
    note: str

    @property
    def has_parking(self) -> bool:
        return self.parking_ping > 0

    @property
    def parking_split(self) -> bool:
        """車位有單獨申報價格，房屋單價才算得準。"""
        return not self.has_parking or self.parking_wan > 0

    @property
    def special(self) -> bool:
        return any(k in self.note for k in ("親友", "債務", "急買", "急賣", "分期", "含增建", "瑕疵"))


def normalize_address(text: str) -> str:
    return (text or "").translate(_FULLWIDTH_DIGITS).replace("臺", "台").replace(" ", "")


def _to_float(value: str) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _roc_year(value: str) -> Optional[int]:
    """建築完成年月為民國 yyyMMdd 或 yyMMdd，取民國年。"""
    digits = "".join(c for c in (value or "") if c.isdigit())
    if len(digits) < 5:
        return None
    year = int(digits[:-4])
    return year if 30 <= year <= 200 else None


def _floor_to_int(text: str) -> Optional[int]:
    text = (text or "").translate(_FULLWIDTH_DIGITS)
    digits = "".join(c for c in text if c.isdigit())
    if digits:
        return int(digits)
    # 「十二層」「六層」這種中文層數
    total = 0
    if "十" in text:
        head, _, tail = text.partition("十")
        total = (_CN_NUMERALS.get(head, 1) if head else 1) * 10
        total += _CN_NUMERALS.get(tail[:1], 0) if tail else 0
        return total
    for char in text:
        if char in _CN_NUMERALS:
            return _CN_NUMERALS[char]
    return None


def recent_seasons(count: int = 8, today: Optional[date] = None) -> list[str]:
    """由近而遠列出民國季別，例如 ['115S2', '115S1', '114S4', ...]。"""
    today = today or date.today()
    year = today.year - 1911
    quarter = (today.month - 1) // 3 + 1
    seasons = []
    for _ in range(count):
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
        seasons.append(f"{year}S{quarter}")
    return seasons


def _ssl_context() -> ssl.SSLContext:
    # plvr.land.moi.gov.tw 的憑證缺 Subject Key Identifier，預設驗證會失敗
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _cache_path(season: str, city_code: str) -> Path:
    return CACHE_DIR / f"{season}_{city_code}.csv"


def fetch_season_csv(season: str, city_code: str, timeout: int = 120) -> str:
    """下載該季全國 zip，只留下需要的縣市 CSV 存進快取。"""
    path = _cache_path(season, city_code)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")

    url = DOWNLOAD_URL.format(season=season)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (houseapp)"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as response:
        raw = response.read()

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = f"{city_code}_lvr_land_a.csv"
        if name not in archive.namelist():
            raise FileNotFoundError(f"{season} 找不到 {name}")
        text = archive.read(name).decode("utf-8", "ignore")

    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def parse_season_csv(text: str, season: str) -> list[LandDeal]:
    reader = csv.DictReader(io.StringIO(text))
    deals = []
    for row in reader:
        raw_date = (row.get("交易年月日") or "").strip()
        if not raw_date.isdigit() or len(raw_date) < 6:
            continue  # 第二列是英文欄名
        total_wan = _to_float(row.get("總價元")) / 10000
        parking_wan = _to_float(row.get("車位總價元")) / 10000
        total_ping = _to_float(row.get("建物移轉總面積平方公尺")) / SQM_PER_PING
        parking_ping = _to_float(row.get("車位移轉總面積平方公尺")) / SQM_PER_PING
        house_ping = total_ping - parking_ping
        if total_wan <= 0 or house_ping <= 0:
            continue

        unit_price = None
        if parking_ping <= 0 or parking_wan > 0:
            unit_price = round((total_wan - parking_wan) / house_ping, 2)

        rooms = (row.get("建物現況格局-房") or "").strip()
        halls = (row.get("建物現況格局-廳") or "").strip()
        deals.append(
            LandDeal(
                date=f"{raw_date[:-4]}-{raw_date[-4:-2]}",
                address=normalize_address(row.get("土地位置建物門牌")),
                floor=(row.get("移轉層次") or "").strip(),
                floor_no=_floor_to_int(row.get("移轉層次")),
                total_floor=_floor_to_int(row.get("總樓層數")),
                total_wan=round(total_wan, 1),
                parking_wan=round(parking_wan, 1),
                total_ping=round(total_ping, 2),
                parking_ping=round(parking_ping, 2),
                house_ping=round(house_ping, 2),
                house_unit_price=unit_price,
                parking_type=(row.get("車位類別") or "").strip(),
                layout=f"{rooms}房{halls}廳" if rooms else "",
                building_type=(row.get("建物型態") or "").strip(),
                build_year=_roc_year(row.get("建築完成年月")),
                note=(row.get("備註") or "").strip(),
            )
        )
    return deals


_MEMORY_CACHE: dict = {}


def load_city_deals(city: str, seasons: Optional[list[str]] = None) -> tuple[list[LandDeal], list[str]]:
    """載入指定縣市近幾季的買賣成交，回傳 (成交清單, 實際取得的季別)。"""
    city_code = CITY_FILE_CODES.get(normalize_address(city))
    if not city_code:
        return [], []
    seasons = seasons or recent_seasons()
    key = (city_code, tuple(seasons))
    if key in _MEMORY_CACHE:
        return _MEMORY_CACHE[key]

    deals: list[LandDeal] = []
    loaded: list[str] = []
    for season in seasons:
        try:
            text = fetch_season_csv(season, city_code)
        except Exception:
            continue
        deals.extend(parse_season_csv(text, season))
        loaded.append(season)

    _MEMORY_CACHE[key] = (deals, loaded)
    return deals, loaded


def building_key(address: str) -> str:
    """取到門牌號為止，例如「台北市南港區經貿一路59號五樓」→「…經貿一路59號」。"""
    text = normalize_address(address)
    match = re.match(r"^(.*?\d+號)", text)
    return match.group(1) if match else text


def door_number(address: str) -> Optional[int]:
    match = re.search(r"(\d+)號", normalize_address(address))
    return int(match.group(1)) if match else None


def find_building_deals(
    deals: list[LandDeal],
    district: str,
    road: str,
    numbers: set[int],
    total_floor: Optional[int] = None,
) -> tuple[list[LandDeal], str]:
    """框出「同一棟」的成交。

    一個社區常橫跨數個門牌（例如 55、57、59 號），591 與樂居又可能各記其中一個，
    所以門牌完全相同時直接採用，否則退而求其次用同路段＋相鄰門牌＋同樓高界定。
    """
    district = normalize_address(district)
    road = normalize_address(road)
    if not district or not road:
        return [], ""

    pool = [d for d in deals if district in d.address and road in d.address]
    if not pool:
        return [], ""

    exact = [d for d in pool if door_number(d.address) in numbers]
    if exact:
        return exact, "同門牌"

    if total_floor and numbers:
        near = [
            d
            for d in pool
            if d.total_floor == total_floor
            and (door_number(d.address) or -999) != -999
            and any(abs(door_number(d.address) - n) <= 10 for n in numbers)
        ]
        if near:
            return near, f"同路段相鄰門牌且同為 {total_floor} 層"

    if total_floor:
        same_floor = [d for d in pool if d.total_floor == total_floor]
        if same_floor:
            return same_floor, f"同路段且同為 {total_floor} 層"
    return [], ""


def find_comparables(
    deals: list[LandDeal],
    district: str,
    building_deals: list[LandDeal],
    road: str,
    total_floor: Optional[int] = None,
    build_year: Optional[int] = None,
    building_type: str = "",
) -> tuple[list[LandDeal], str]:
    """由嚴到寬找可比較的成交，回傳 (成交清單, 比對層級說明)。

    同棟成交通常只有個位數，樣本不足時往同路段、同行政區放寬，
    但放寬時會補上屋齡與建物型態條件，避免拿新成屋去比中古華廈。
    """
    district = normalize_address(district)
    in_district = [d for d in deals if district and district in d.address]

    def usable(items: list[LandDeal]) -> int:
        return len(usable_unit_prices(items))

    if usable(building_deals) >= 5:
        return building_deals, "同棟成交"

    conditions = []
    if build_year:
        conditions.append(
            (
                f"屋齡相近（民國 {build_year} 年落成前後）",
                lambda d: bool(d.build_year) and abs(d.build_year - build_year) <= 8,
            )
        )
    if building_type:
        kind = building_type.split("(")[0]
        if kind:
            conditions.append((kind, lambda d: kind in d.building_type))
    if total_floor:
        conditions.append(
            (
                f"{total_floor} 層上下",
                lambda d: bool(d.total_floor) and abs(d.total_floor - total_floor) <= 2,
            )
        )

    def narrow(pool: list[LandDeal], base: str, minimum: int) -> tuple[list[LandDeal], str, int]:
        """依序套用條件，套用後樣本仍夠才保留，避免比對範圍過度發散。"""
        labels = [base]
        for label, predicate in conditions:
            candidate = [d for d in pool if predicate(d)]
            if usable(candidate) >= minimum:
                pool = candidate
                labels.append(label)
        return pool, "、".join(labels), usable(pool)

    same_road = [d for d in in_district if road and road in d.address]
    road_pool, road_label, road_n = narrow(same_road, "同路段", 5)
    if road_n >= 5:
        return road_pool, road_label

    district_pool, district_label, district_n = narrow(in_district, district, 8)
    if district_n >= 8:
        return district_pool, district_label

    if building_deals:
        return building_deals, "同棟成交（筆數偏少）"
    return in_district, district


def usable_unit_prices(deals: list[LandDeal]) -> list[float]:
    """可用來算行情的單價：車位已拆分、非特殊交易、且落在合理範圍。"""
    return sorted(
        d.house_unit_price
        for d in deals
        if d.house_unit_price and not d.special and 10 <= d.house_unit_price <= 300
    )


def unit_price_quartiles(deals: list[LandDeal]) -> tuple[Optional[float], Optional[float], Optional[float], int]:
    """回傳房屋單價（已扣除車位）的 25%、中位、75% 與樣本數。"""
    values = usable_unit_prices(deals)
    if not values:
        return None, None, None, 0
    if len(values) < 4:
        return values[0], round(statistics.median(values), 1), values[-1], len(values)
    return (
        values[len(values) // 4],
        round(statistics.median(values), 1),
        values[(3 * len(values)) // 4],
        len(values),
    )


def parking_price_range(
    deals: list[LandDeal], district: str, keywords: tuple[str, ...] = ()
) -> tuple[Optional[int], Optional[int], int, str]:
    """依同行政區、同車位類別的官方申報車位總價抓區間。

    keywords 是可接受的車位類別關鍵字，例如平面車位可傳 ("坡道平面", "升降平面", "一樓平面")。
    """
    district = normalize_address(district)
    pool = [d for d in deals if district in d.address and d.has_parking and d.parking_wan > 0]
    scope = f"{district}車位"
    if keywords:
        typed = [d for d in pool if any(k in d.parking_type for k in keywords)]
        if len(typed) >= 10:
            pool = typed
            scope = f"{district}{'／'.join(keywords)}車位"
    if not pool:
        return None, None, 0, ""

    values = sorted(d.parking_wan for d in pool)
    low = values[len(values) // 4]
    high = values[(3 * len(values)) // 4]
    note = f"依實價登錄{scope} {len(values)} 筆申報價（中位 {statistics.median(values):.0f} 萬）"
    return round(low), round(high), len(values), note


def find_previous_sale(
    building_deals: list[LandDeal],
    total_ping: Optional[float],
    parking_ping: Optional[float] = None,
    floor_no: Optional[int] = None,
) -> tuple[Optional[LandDeal], bool]:
    """在同棟成交中指認本戶的上一次成交，回傳 (成交, 是否確定為本戶)。

    同一棟常有多戶坪數相同，光靠坪數會抓到隔壁戶，因此樓層與車位坪數都是硬性條件；
    樓層不明、或同樓層仍有多筆同坪數成交時，只能說是「疑似」，交由呼叫端據實標示。
    """
    if not total_ping:
        return None, False

    matches = [d for d in building_deals if abs(d.total_ping - total_ping) <= 0.3]
    matches = [d for d in matches if abs(d.parking_ping - (parking_ping or 0)) <= 0.3]
    if floor_no:
        matches = [d for d in matches if d.floor_no == floor_no]
    if not matches:
        return None, False

    latest = max(matches, key=lambda d: d.date)
    certain = bool(floor_no) and len({d.address for d in matches}) == 1
    return latest, certain
