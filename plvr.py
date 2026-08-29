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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

CACHE_DIR = Path(__file__).with_name("plvr_cache")
COMMON_WARM_CITIES = ("台北市", "新北市", "桃園市", "台中市", "高雄市", "台南市")
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
    text = (text or "").translate(_FULLWIDTH_DIGITS).replace("臺", "台").replace(" ", "")
    return text.replace("－", "-").replace("–", "-")


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
    """把實價登錄的「移轉層次」轉成樓層數字。地下為負、全層／多層一次移轉回傳 None。"""
    text = (text or "").translate(_FULLWIDTH_DIGITS)
    if re.search(r"全層|見使用|整棟", text):
        return None
    # 「一層，二層」是一次移轉多層；「七層，電梯樓梯間」仍是單層，不能看到逗號就整筆丟掉。
    layer_tokens = re.findall(r"[一二三四五六七八九十百]+\s*層|\d+\s*層", text)
    if len(layer_tokens) >= 2:
        return None

    underground = bool(re.search(r"地下|B\s*\d", text, re.I))
    token = layer_tokens[0] if layer_tokens else text
    chinese = _chinese_floor(token)
    if chinese is not None:
        return -chinese if underground else chinese

    match = re.search(r"(\d+)", token)
    if match:
        value = int(match.group(1))
        return -value if underground else value
    return None


def _chinese_floor(text: str) -> Optional[int]:
    if "十" in text:
        head, _, tail = text.partition("十")
        total = (_CN_NUMERALS.get(head[:1], 1) if head and head[:1] in _CN_NUMERALS else 1) * 10
        total += _CN_NUMERALS.get(tail[:1], 0) if tail and tail[:1] in _CN_NUMERALS else 0
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


def _season_zip_path(season: str) -> Path:
    return CACHE_DIR / f"{season}_all.zip"


def _download_season_zip(season: str, timeout: int = 120) -> bytes:
    """下載並快取該季全國 zip，同季多縣市共用，避免重複下載。"""
    path = _season_zip_path(season)
    if path.exists():
        return path.read_bytes()

    url = DOWNLOAD_URL.format(season=season)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (houseapp)"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as response:
        raw = response.read()

    CACHE_DIR.mkdir(exist_ok=True)
    path.write_bytes(raw)
    return raw


def _extract_city_csv(season: str, city_code: str, raw: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        name = f"{city_code}_lvr_land_a.csv"
        if name not in archive.namelist():
            raise FileNotFoundError(f"{season} 找不到 {name}")
        return archive.read(name).decode("utf-8", "ignore")


def fetch_season_csv(season: str, city_code: str, timeout: int = 120) -> str:
    """下載該季全國 zip，只留下需要的縣市 CSV 存進快取。"""
    path = _cache_path(season, city_code)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")

    raw = _download_season_zip(season, timeout=timeout)
    text = _extract_city_csv(season, city_code, raw)

    CACHE_DIR.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return text


def warm_common_cities(season_count: int = 2) -> dict[str, str]:
    """預載六都最近幾季實價登錄，雲端首次分析會快很多。"""
    status: dict[str, str] = {}
    for season in recent_seasons(season_count):
        try:
            raw = _download_season_zip(season)
        except Exception as exc:
            status[f"season:{season}"] = f"失敗：{exc}"
            continue
        for city in COMMON_WARM_CITIES:
            code = CITY_FILE_CODES.get(normalize_address(city))
            if not code:
                continue
            try:
                text = _extract_city_csv(season, code, raw)
                CACHE_DIR.mkdir(exist_ok=True)
                _cache_path(season, code).write_text(text, encoding="utf-8")
                status[city] = "ready"
            except Exception as exc:
                status[city] = f"部分失敗：{exc}"
    return status


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

    def _load_season(season: str) -> tuple[str, list[LandDeal]] | None:
        try:
            text = fetch_season_csv(season, city_code)
        except Exception:
            return None
        return season, parse_season_csv(text, season)

    workers = min(3, len(seasons))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_load_season, season) for season in seasons]
        for future in as_completed(futures):
            result = future.result()
            if not result:
                continue
            season, season_deals = result
            deals.extend(season_deals)
            loaded.append(season)
    loaded.sort(key=lambda s: seasons.index(s) if s in seasons else 999)

    _MEMORY_CACHE[key] = (deals, loaded)
    return deals, loaded


def building_key(address: str) -> str:
    """取到門牌號為止，例如「台北市南港區經貿一路59號五樓」→「…經貿一路59號」。"""
    text = _normalize_subno(normalize_address(address))
    match = re.match(r"^(.*?\d+(?:之\d+)?號)", text)
    return match.group(1) if match else text


def _normalize_subno(text: str) -> str:
    """刊登常用 54-5號，實價登錄是 54之5號。"""
    return re.sub(r"(\d+)-(\d+)號", r"\1之\2號", text)


def parse_house_number(address: str) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """拆門牌為 (巷, 弄, 號, 之號)。54-5號／54之5號 → (None, None, 54, 5)。"""
    text = _normalize_subno(normalize_address(address))
    match = re.search(r"(?:(\d+)巷)?(?:(\d+)弄)?(\d+)(?:之(\d+))?號", text)
    if not match:
        return None, None, None, None
    lane = int(match.group(1)) if match.group(1) else None
    alley = int(match.group(2)) if match.group(2) else None
    door = int(match.group(3)) if match.group(3) else None
    sub = int(match.group(4)) if match.group(4) else None
    return lane, alley, door, sub


def door_number(address: str) -> Optional[int]:
    """主門牌號。54之5號、54-5號都回 54，不會誤取之號的 5。"""
    _lane, _alley, door, _sub = parse_house_number(address)
    return door


def _same_building_cluster(deal_address: str, listing_addresses: list[str]) -> bool:
    """同社區門牌：54號／54之5號算同一組；189巷54號、54巷透天則不算。"""
    d_lane, d_alley, d_door, _d_sub = parse_house_number(deal_address)
    if d_door is None:
        return False
    for raw in listing_addresses:
        l_lane, l_alley, l_door, _l_sub = parse_house_number(raw)
        if l_door is None and l_lane is None:
            continue
        if l_lane is not None:
            if d_lane == l_lane and (l_alley is None or d_alley == l_alley):
                if l_door is None or d_door == l_door:
                    return True
            continue
        if l_door is None:
            continue
        if d_lane is None and d_door == l_door:
            return True
        if d_lane == l_door and (l_alley is None or d_alley == l_alley):
            return True
    return False


def _compatible_total_floor(deal_floor: Optional[int], total_floor: Optional[int]) -> bool:
    if not total_floor or not deal_floor:
        return True
    return abs(deal_floor - total_floor) <= 3


def find_building_deals(
    deals: list[LandDeal],
    district: str,
    road: str,
    numbers: set[int],
    total_floor: Optional[int] = None,
    addresses: Optional[list[str]] = None,
) -> tuple[list[LandDeal], str]:
    """框出「同一棟／同一社區門牌」的成交。

    一個社區常橫跨 54號、54之5號、54巷；但不能把「康寧路三段189巷54號」
    當成「康寧路三段54號」。沒找到同社區門牌時，才用同路段、無巷弄、相鄰門牌。
    不再用「整條路同樓高」當同棟，那會把便宜很多的別社區算進來。
    """
    district = normalize_address(district)
    road = normalize_address(road)
    if not district or not road:
        return [], ""

    pool = [d for d in deals if district in d.address and road in d.address]
    if not pool:
        return [], ""

    listing_addrs = [a for a in (addresses or []) if a]
    cluster = [d for d in pool if listing_addrs and _same_building_cluster(d.address, listing_addrs)]
    if not cluster and numbers:
        cluster = [
            d
            for d in pool
            if parse_house_number(d.address)[0] is None and door_number(d.address) in numbers
        ]
    if cluster:
        floored = [d for d in cluster if _compatible_total_floor(d.total_floor, total_floor)]
        if floored:
            return floored, "同社區門牌"
        if not total_floor:
            return cluster, "同社區門牌"

    if total_floor and numbers:
        near = [
            d
            for d in pool
            if d.total_floor == total_floor
            and parse_house_number(d.address)[0] is None
            and door_number(d.address) is not None
            and any(abs((door_number(d.address) or 0) - n) <= 10 for n in numbers)
        ]
        if near:
            return near, f"同路段相鄰門牌且同為 {total_floor} 層"
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

    if usable(building_deals) >= 1:
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
    if len(values) == 1:
        value = values[0]
        return round(value * 0.95, 2), round(value, 1), round(value * 1.05, 2), 1
    # 同棟只有幾筆時，四分位會把最新高價裁掉，看起來像估太低。
    if len(values) < 8:
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
    parking_known: bool = True,
) -> tuple[Optional[LandDeal], bool]:
    """在同棟成交中指認本戶的上一次成交，回傳 (成交, 是否確定為本戶)。

    同一棟常有多戶坪數相同，光靠坪數會抓到隔壁戶，因此樓層與車位坪數都是硬性條件；
    樓層不明、或同樓層仍有多筆同坪數成交時，只能說是「疑似」，交由呼叫端據實標示。
    刊登有車位但未獨立標示坪數時 parking_known=False，此時不把車位當成 0 坪硬篩。
    """
    if not total_ping:
        return None, False

    matches = [d for d in building_deals if abs(d.total_ping - total_ping) <= 0.3]
    if parking_known:
        matches = [d for d in matches if abs(d.parking_ping - (parking_ping or 0)) <= 0.3]
    if floor_no:
        matches = [d for d in matches if d.floor_no == floor_no]
    if not matches:
        return None, False

    latest = max(matches, key=lambda d: d.date)
    certain = bool(floor_no) and len({d.address for d in matches}) == 1
    return latest, certain
