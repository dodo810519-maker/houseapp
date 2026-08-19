import html as html_module
import io
import json
import math
import os
import re
import ssl
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import plvr

try:
    from PIL import Image
except ImportError:
    Image = None


UNKNOWN = "查無資料"
UNDETERMINED = "無法判斷"

NEARBY_CACHE_PATH = Path(__file__).with_name("nearby_cache.json")

CONVENIENCE_NAMES = ("全家", "7-eleven", "7-eleven", "7-11", "統一超商", "ok超商", "ok mart", "萊爾富", "hi-life")
SUPERMARKET_HINTS = ("全聯", "美廉社", "家樂福", "大潤發", "愛買", "頂好", "松青", "jasons", "costco", "好市多", "超市", "主婦聯盟", "pxmart", "px mart")


@dataclass
class Deal:
    date: str
    floor: str
    unit_price: Optional[float]
    total_wan: Optional[float]
    layout: str
    area_ping: Optional[float]
    parking: str
    address: str
    special: bool = False


@dataclass
class NearbyPlace:
    name: str
    meters: int


@dataclass
class AnalysisReport:
    title: str
    listing_id: str
    source_url: str
    image_url: str
    ask_price_wan: Optional[float]
    ask_unit_price: str
    layout: str
    community_name: str
    land_area: str
    household_count: str
    building_floors: str
    public_ratio: str
    community_address: str
    listing_address: str
    nearest_mrt: str
    nearest_supermarket: str
    registered_area: str
    parking_status: str
    house_age: str
    main_building_area: str
    registered_use: str
    floor: str
    bathroom_window: str
    lighting_faces: str
    unit_price_range: str
    total_price_range: str
    market_comment: str
    previous_sale: str = UNKNOWN
    building_deals: list = field(default_factory=list)
    building_deal_scope: str = ""
    interior_images: list[str] = field(default_factory=list)
    community_images: list[str] = field(default_factory=list)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    deals: list[Deal] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch(url: str, accept: str = "text/html,application/json;q=0.9,*/*;q=0.8") -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": accept,
            "Accept-Language": "zh-TW,zh;q=0.9",
            "device": "pc",
            "deviceid": "houseapp-local-001",
            "Referer": "https://sale.591.com.tw/",
        },
    )
    with urllib.request.urlopen(req, timeout=25, context=_ssl_context()) as response:
        return response.read().decode("utf-8", "ignore")


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _or_unknown(value: Optional[str]) -> str:
    value = _clean(value or "")
    return value if value else UNKNOWN


def _info_map(items: list) -> dict:
    result = {}
    for item in items or []:
        name = _clean(str(item.get("name", "")))
        value = _clean(str(item.get("value", "")))
        if name:
            result[name] = value
    return result


def _parse_591_id(url: str) -> str:
    """接受電腦版、手機版、以及只貼物件編號。"""
    text = (url or "").strip()
    if re.fullmatch(r"\d{6,}", text):
        return text
    match = re.search(r"(?:sale|m|house)\.591\.com\.tw/.*?(\d{6,})", text)
    if not match:
        match = re.search(r"/(\d{6,})\.html", text)
    if not match:
        match = re.search(r"(?:id|house_id|post_id)=(\d{6,})", text)
    if not match:
        raise ValueError("這不是有效的 591 售屋網址，請確認後再試。")
    return match.group(1)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    radius = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * radius * math.asin(math.sqrt(a)))


def _walk_text(meters: int) -> str:
    minutes = max(1, round(meters / 80))
    return f"約 {meters} 公尺（步行約 {minutes} 分鐘）"


def _google_maps_api_key() -> str:
    return os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()


def _google_get_json(url: str, timeout: int = 15) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "houseapp/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def _google_post_json(url: str, body: dict, headers: dict, timeout: int = 15) -> Optional[dict]:
    try:
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={**headers, "Content-Type": "application/json", "User-Agent": "houseapp/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def geocode_google(address: str, api_key: str, timeout: int = 15) -> tuple[Optional[float], Optional[float]]:
    if not address or address == UNKNOWN:
        return None, None
    query = urllib.parse.quote(address.split("（")[0].strip())
    url = (
        "https://maps.googleapis.com/maps/api/geocode/json?"
        f"address={query}&language=zh-TW&region=tw&key={urllib.parse.quote(api_key)}"
    )
    data = _google_get_json(url, timeout=timeout)
    if not data or data.get("status") != "OK" or not data.get("results"):
        return None, None
    loc = data["results"][0]["geometry"]["location"]
    return _to_float(loc.get("lat")), _to_float(loc.get("lng"))


def _google_walking_distances(
    origin_lat: float,
    origin_lon: float,
    destinations: list[tuple[float, float]],
    api_key: str,
    timeout: int = 15,
) -> list[tuple[Optional[int], str]]:
    if not destinations:
        return []
    dest_text = "|".join(f"{lat},{lon}" for lat, lon in destinations)
    url = (
        "https://maps.googleapis.com/maps/api/distancematrix/json?"
        f"origins={origin_lat},{origin_lon}&destinations={dest_text}"
        f"&mode=walking&language=zh-TW&key={urllib.parse.quote(api_key)}"
    )
    data = _google_get_json(url, timeout=timeout)
    results: list[tuple[Optional[int], str]] = []
    if not data or data.get("status") != "OK":
        return [(None, "") for _ in destinations]
    elements = (data.get("rows") or [{}])[0].get("elements") or []
    for element in elements:
        if element.get("status") != "OK":
            results.append((None, ""))
            continue
        meters = element.get("distance", {}).get("value")
        duration = element.get("duration", {}).get("text") or ""
        results.append((int(meters) if meters is not None else None, duration))
    while len(results) < len(destinations):
        results.append((None, ""))
    return results[: len(destinations)]


def _google_place_name(place: dict) -> str:
    display = place.get("displayName") or {}
    return _clean(display.get("text") or "")


def _google_place_location(place: dict) -> tuple[Optional[float], Optional[float]]:
    loc = place.get("location") or {}
    return _to_float(loc.get("latitude")), _to_float(loc.get("longitude"))


def _google_search_text(
    text_query: str,
    lat: float,
    lon: float,
    api_key: str,
    radius: int = 2500,
    timeout: int = 15,
) -> list[dict]:
    data = _google_post_json(
        "https://places.googleapis.com/v1/places:searchText",
        {
            "textQuery": text_query,
            "languageCode": "zh-TW",
            "locationBias": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": float(radius),
                }
            },
        },
        {
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.location,places.types",
        },
        timeout=timeout,
    )
    return (data or {}).get("places") or []


def _google_search_nearby(
    included_types: list[str],
    lat: float,
    lon: float,
    api_key: str,
    radius: int = 1500,
    max_results: int = 8,
    timeout: int = 15,
) -> list[dict]:
    data = _google_post_json(
        "https://places.googleapis.com/v1/places:searchNearby",
        {
            "includedTypes": included_types,
            "maxResultCount": max_results,
            "languageCode": "zh-TW",
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lon},
                    "radius": float(radius),
                }
            },
        },
        {
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "places.displayName,places.location,places.types",
        },
        timeout=timeout,
    )
    return (data or {}).get("places") or []


def _format_google_nearby(name: str, meters: int, duration: str = "") -> str:
    if duration:
        return f"{name}，步行 {duration} / 約 {meters} 公尺（Google 地圖）"
    return f"{name}，{_walk_text(meters)}（Google 地圖）"


def _pick_nearest_google_place(
    origin_lat: float,
    origin_lon: float,
    places: list[dict],
    api_key: str,
    name_filter=None,
    timeout: int = 15,
) -> Optional[str]:
    candidates: list[tuple[str, float, float]] = []
    for place in places:
        name = _google_place_name(place)
        plat, plon = _google_place_location(place)
        if not name or plat is None or plon is None:
            continue
        if name_filter and not name_filter(name):
            continue
        candidates.append((name, plat, plon))
    if not candidates:
        return None

    distances = _google_walking_distances(
        origin_lat,
        origin_lon,
        [(lat, lon) for _, lat, lon in candidates],
        api_key,
        timeout=timeout,
    )
    ranked: list[tuple[int, str, str]] = []
    for (name, plat, plon), (meters, duration) in zip(candidates, distances):
        if meters is None:
            meters = _haversine_m(origin_lat, origin_lon, plat, plon)
        ranked.append((meters, name, duration))
    ranked.sort(key=lambda item: item[0])
    meters, name, duration = ranked[0]
    return _format_google_nearby(name, meters, duration)


def fetch_nearby_google(
    lat: float,
    lon: float,
    api_key: str,
    timeout: int = 15,
) -> tuple[str, str]:
    mrt_places = _google_search_text("捷運站", lat, lon, api_key, radius=2500, timeout=timeout)
    if not mrt_places:
        mrt_places = _google_search_nearby(
            ["subway_station", "transit_station", "light_rail_station"],
            lat,
            lon,
            api_key,
            radius=2500,
            timeout=timeout,
        )

    def is_mrt(name: str) -> bool:
        if "公車" in name or "YouBike" in name or "Ubike" in name:
            return False
        return "捷運" in name or name.endswith("站")

    shop_places = _google_search_nearby(
        ["supermarket", "grocery_store"],
        lat,
        lon,
        api_key,
        radius=1500,
        max_results=10,
        timeout=timeout,
    )

    def is_supermarket(name: str) -> bool:
        lower = name.lower()
        if any(x in lower for x in CONVENIENCE_NAMES):
            return False
        return any(x in lower or x in name for x in SUPERMARKET_HINTS)

    mrt_text = _pick_nearest_google_place(lat, lon, mrt_places, api_key, is_mrt, timeout) or UNKNOWN
    shop_text = _pick_nearest_google_place(lat, lon, shop_places, api_key, is_supermarket, timeout) or UNKNOWN
    return mrt_text, shop_text


def _to_float(text: str) -> Optional[float]:
    if text is None:
        return None
    match = re.search(r"[\d,]+(?:\.\d+)?", str(text).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def fetch_591_detail(house_id: str) -> dict:
    raw = _fetch(
        f"https://api.591.com.tw/tw/v1/house/sale/detail?id={house_id}",
        accept="application/json",
    )
    data = json.loads(raw)
    if data.get("status") != 1 or not data.get("data"):
        raise ValueError("591 房屋資料讀取失敗，請稍後再試。")
    return data["data"]


def _parse_unit_price(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"([\d.]+)\s*萬/坪", text)
    return float(match.group(1)) if match else None


def fetch_community_id(house_id: str) -> str:
    html = _fetch(f"https://sale.591.com.tw/home/house/detail/2/{house_id}.html")
    match = re.search(r'id="hid_communityId"\s+value="(\d+)"', html)
    if match and match.group(1) not in {"", "0"}:
        return match.group(1)
    for cid in re.findall(r"market\.591\.com\.tw/(\d+)", html):
        if cid not in {"0"} and len(cid) >= 4:
            return cid
    return ""


def parse_community_page(html: str) -> dict:
    result = {
        "name": "",
        "land_area": "",
        "household_count": "",
        "building_floors": "",
        "public_ratio": "",
        "address": "",
        "avg_unit": "",
        "deals": [],
    }

    ld_match = re.search(r"application/ld\+json[^>]*>(.*?)</script>", html, re.S)
    if ld_match:
        try:
            ld = json.loads(ld_match.group(1))
            for item in ld.get("@graph", []):
                types = item.get("@type", [])
                if isinstance(types, str):
                    types = [types]
                if "ApartmentComplex" in types:
                    result["name"] = item.get("name", "")
                    address = item.get("address", {})
                    if isinstance(address, dict):
                        result["address"] = (
                            address.get("streetAddress")
                            or "".join(
                                [
                                    address.get("addressRegion", ""),
                                    address.get("addressLocality", ""),
                                ]
                            )
                        )
                    units = item.get("numberOfAccommodationUnits", {})
                    if isinstance(units, dict) and units.get("value"):
                        result["household_count"] = f"{units['value']} 戶"
                if item.get("@type") == "WebPage":
                    name = item.get("name", "")
                    avg = re.search(r"均價([\d.]+)萬/坪", name)
                    if avg:
                        result["avg_unit"] = f"{avg.group(1)} 萬/坪"
        except json.JSONDecodeError:
            pass

    for m in re.finditer(
        r'class="label"[^>]*>\s*(.*?)\s*</h3>\s*<span class="text[^"]*"[^>]*>(.*?)</span>',
        html,
        re.S,
    ):
        label = _clean(m.group(1))
        value = _clean(m.group(2))
        if label == "總戶數" and not result["household_count"]:
            result["household_count"] = value
        if label == "地址" and not result["address"]:
            result["address"] = re.sub(r"^(文德|港墘|西湖|內湖)\s*", "", value)

    desc = html
    land = re.search(r"基地面積\s*([\d,.]+)\s*坪", desc)
    if land:
        result["land_area"] = f"{land.group(1)} 坪"
    else:
        land = re.search(r'"([\d,]{3,})\s*坪"', desc)
        if land and result["name"]:
            result["land_area"] = f"{land.group(1)} 坪"

    floors = re.search(r"地上\s*(\d+)\s*層[，,]?\s*地下\s*(\d+)\s*層", desc)
    if floors:
        result["building_floors"] = f"地上 {floors.group(1)} 層 / 地下 {floors.group(2)} 層"
    buildings = re.search(r"(\d+)棟", desc)
    if buildings and result["building_floors"] != UNKNOWN and result["building_floors"]:
        result["building_floors"] += f"（共 {buildings.group(1)} 棟）"

    ratio = re.search(r"公設(?:比)?\s*([\d.~～\-]+)\s*%", desc)
    if ratio:
        result["public_ratio"] = f"{ratio.group(1)}%"
    elif re.search(r"35~39%", desc):
        result["public_ratio"] = "35~39%"

    result["deals"] = parse_community_deals(html)
    if not result["deals"]:
        result["deals"] = parse_community_deals_loose(html)
    return result


def parse_community_deals_loose(html: str) -> list[Deal]:
    deals: list[Deal] = []
    chunks = re.findall(
        r"(\d{3}-\d{2}).{0,200}?([\d.]+)\s*萬/坪.{0,200}?(\d+)房/([\d.]+)坪",
        html,
        re.S,
    )
    seen = set()
    for date, unit, rooms, area in chunks:
        key = (date, unit, area)
        if key in seen:
            continue
        seen.add(key)
        deals.append(
            Deal(
                date=date,
                floor=UNKNOWN,
                unit_price=_to_float(unit),
                total_wan=None,
                layout=f"{rooms}房",
                area_ping=_to_float(area),
                parking=UNKNOWN,
                address=UNKNOWN,
                special=False,
            )
        )
    return deals[:20]


def parse_community_deals(html: str) -> list[Deal]:
    deals: list[Deal] = []
    rows = re.findall(
        r'class="realprice-list-row.*?(?=class="realprice-list-row|</section>)',
        html,
        re.S,
    )
    for row in rows:
        special = "特殊交易" in row
        text = _clean(row)
        date_m = re.search(r"(\d{3}-\d{2})", text)
        floor_m = re.search(r">(\d{1,2}樓(?:之\d+)?)<", row)
        if not floor_m:
            floor_m = re.search(r"(?<![\d-])(\d{1,2}樓(?:之\d+)?)", text)
        unit_m = re.search(r"([\d.]+)\s*萬/坪", text)
        layout_m = re.search(r"(\d+)房/([\d.]+)坪", text)
        parking_m = re.search(r"(無車位|車位[\d.]+坪)", text)
        addr_m = re.search(r"((?:[\u4e00-\u9fff]{2,}路|[\u4e00-\u9fff]+街)[^|]{4,40})", text)
        totals = [t.replace(",", "") for t in re.findall(r"([\d,]+)\s*萬", text)]
        total_wan = None
        for item in totals:
            value = _to_float(item)
            if value and value >= 100:
                total_wan = value
                break
        if not date_m:
            continue
        deals.append(
            Deal(
                date=date_m.group(1),
                floor=floor_m.group(1) if floor_m else UNKNOWN,
                unit_price=_to_float(unit_m.group(1)) if unit_m else None,
                total_wan=total_wan,
                layout=f"{layout_m.group(1)}房" if layout_m else UNKNOWN,
                area_ping=_to_float(layout_m.group(2)) if layout_m else None,
                parking=parking_m.group(1) if parking_m else UNKNOWN,
                address=_clean(addr_m.group(1)) if addr_m else UNKNOWN,
                special=special,
            )
        )
    return deals


def _query_overpass(query: str, timeout: int = 12) -> Optional[dict]:
    encoded = urllib.parse.quote(query)
    try:
        req = urllib.request.Request(
            "https://overpass-api.de/api/interpreter",
            data=f"data={encoded}".encode("utf-8"),
            method="POST",
            headers={
                "User-Agent": "houseapp/1.0 (591 property analysis)",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "*/*",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8", "ignore"))
    except Exception:
        return None


def _load_nearby_cache() -> dict:
    try:
        return json.loads(NEARBY_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_nearby_cache(cache: dict) -> None:
    try:
        NEARBY_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def fetch_nearby_cached(
    lat: float,
    lon: float,
    timeout: int = 12,
    api_key: str = "",
) -> tuple[str, str]:
    api_key = api_key or _google_maps_api_key()
    cache_key = f"google:{lat:.4f},{lon:.4f}" if api_key else f"osm:{lat:.3f},{lon:.3f}"
    cache = _load_nearby_cache()
    cached = cache.get(cache_key)
    if isinstance(cached, list) and len(cached) == 2:
        return cached[0], cached[1]

    mrt, shop = UNKNOWN, UNKNOWN
    if api_key:
        mrt, shop = fetch_nearby_google(lat, lon, api_key, timeout=timeout)
    if mrt == UNKNOWN and shop == UNKNOWN:
        mrt, shop = fetch_nearby(lat, lon, timeout=timeout)

    if mrt != UNKNOWN or shop != UNKNOWN:
        cache[cache_key] = [mrt, shop]
        _save_nearby_cache(cache)
    return mrt, shop


def fetch_nearby(lat: float, lon: float, timeout: int = 12) -> tuple[str, str]:
    query = (
        f"[out:json][timeout:25];("
        f'node["station"="subway"](around:2500,{lat},{lon});'
        f'node["railway"="station"]["station"="subway"](around:2500,{lat},{lon});'
        f'node["public_transport"="station"]["subway"="yes"](around:2500,{lat},{lon});'
        f'node["shop"="supermarket"](around:1500,{lat},{lon});'
        f");out;"
    )
    data = _query_overpass(query, timeout=timeout)
    if not data:
        return UNKNOWN, UNKNOWN

    mrt: list[NearbyPlace] = []
    shops: list[NearbyPlace] = []
    seen_mrt = set()
    seen_shop = set()

    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:zh") or tags.get("brand") or ""
        if not name or "lat" not in el or "lon" not in el:
            continue
        meters = _haversine_m(lat, lon, float(el["lat"]), float(el["lon"]))
        is_subway = tags.get("station") == "subway" or tags.get("subway") == "yes"
        shop_type = tags.get("shop")
        lower = name.lower()

        if is_subway:
            key = name.replace("站", "")
            if key not in seen_mrt:
                seen_mrt.add(key)
                mrt.append(NearbyPlace(name=f"{key}捷運站", meters=meters))
        elif shop_type == "supermarket":
            if any(x in lower for x in CONVENIENCE_NAMES):
                continue
            if "spa" in lower:
                continue
            is_named = any(x in lower or x in name for x in SUPERMARKET_HINTS)
            if not is_named:
                continue
            if name not in seen_shop or meters < next((s.meters for s in shops if s.name == name), 10**9):
                seen_shop.add(name)
                shops = [s for s in shops if s.name != name]
                shops.append(NearbyPlace(name=name, meters=meters))

    mrt.sort(key=lambda x: x.meters)
    shops.sort(key=lambda x: x.meters)

    mrt_text = UNKNOWN
    shop_text = UNKNOWN
    if mrt:
        nearest = mrt[0]
        mrt_text = f"{nearest.name}，{_walk_text(nearest.meters)}"
    if shops:
        nearest = shops[0]
        shop_text = f"{nearest.name}，{_walk_text(nearest.meters)}"
    return mrt_text, shop_text


def _fallback_nearby_from_591(near_info: dict) -> tuple[str, str]:
    traffic = str(near_info.get("traffic") or "")
    living = str(near_info.get("living") or "")

    mrt_text = UNKNOWN
    for part in re.split(r"[、,，；;]", traffic):
        part = part.strip()
        if "捷運" in part:
            mrt_text = f"{part}（591 刊登交通資訊，未計算距離）"
            break
    if mrt_text == UNKNOWN:
        for part in re.split(r"[、,，；;]", traffic):
            part = part.strip()
            if part.endswith("站"):
                mrt_text = f"{part}（591 刊登交通資訊，未計算距離）"
                break

    shop_text = UNKNOWN
    if any(keyword in living for keyword in ("超市", "全聯", "美廉社", "家樂福")):
        shop_text = "附近有超市（591 刊登生活機能，未計算距離）"
    elif "便利商店" in living:
        shop_text = "附近有便利商店（591 刊登生活機能，未計算距離）"

    return mrt_text, shop_text


def detect_bathroom_window(remark_html: str, title: str) -> str:
    text = _clean(_unescape_remark(remark_html)) + " " + (title or "")
    if re.search(r"(暗衛|無對外窗|浴室無窗|衛浴無窗)", text):
        return "無對外窗（依物件文字描述）"
    if re.search(r"(衛浴開窗|浴室開窗|衛浴有窗|浴室有窗|明衛|衛浴對外窗|浴室對外窗|乾濕分離.*開窗)", text):
        return "有對外窗（依物件文字描述）"
    return f"{UNDETERMINED}（物件描述未提及，需看照片或現場確認）"


def fetch_listing_images(house_id: str) -> list[str]:
    html = _fetch(f"https://sale.591.com.tw/home/house/detail/2/{house_id}.html")
    match = re.search(r'id="hid_imgs"\s+value="([^"]+)"', html)
    if not match:
        return []
    try:
        raw = match.group(1).replace("&quot;", '"')
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    urls = []
    seen: set[str] = set()
    for item in items:
        url = item.get("big") or item.get("medium") or item.get("src") or ""
        if not url:
            continue
        if not url.startswith("http"):
            url = f"https:{url}"
        base = url.split("!")[0]
        if base in seen:
            continue
        seen.add(base)
        urls.append(url)
    return urls


COMMUNITY_PUBLIC_KEYWORDS = ("外觀", "環境", "交通", "街景", "公設", "封面")


def parse_community_images(html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"<img[^>]+>", html):
        tag = match.group(0)
        src_match = re.search(r'src="([^"]+)"', tag)
        alt_match = re.search(r'alt="([^"]+)"', tag)
        if not src_match:
            continue
        src = src_match.group(1)
        alt = alt_match.group(1) if alt_match else ""
        if not src.startswith("https://img") or "/user/" in src:
            continue
        if any(skip in alt for skip in ("在售房屋", "樣品屋", "頭像", "專家", "問答")):
            continue
        is_public = any(keyword in alt for keyword in COMMUNITY_PUBLIC_KEYWORDS)
        if not is_public and "/market/" not in src:
            continue
        base = src.split("!")[0]
        if base in seen:
            continue
        seen.add(base)
        if "!900x" in src or "!1200x" in src:
            urls.append(src)
        elif base.endswith(".png"):
            urls.append(src)
        else:
            urls.append(f"{base}!900x.water3.jpg")
    return urls[:12]


def _download_image(url: str) -> Optional["Image.Image"]:
    if Image is None:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://591.com.tw/"},
        )
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as response:
            return Image.open(io.BytesIO(response.read())).convert("RGB")
    except Exception:
        return None


def _lit_bands_in_image(img: "Image.Image") -> int:
    width, height = img.size
    if width < 80 or height < 80:
        return 0
    if max(width, height) > 640:
        scale = 640 / max(width, height)
        img = img.resize((int(width * scale), int(height * scale)))
        width, height = img.size
    pixels = img.load()
    top = int(height * 0.55)
    lit = 0
    for x0, x1 in ((0, width // 3), (width // 3, 2 * width // 3), (2 * width // 3, width)):
        bright = 0
        total = 0
        for y in range(0, top, 5):
            for x in range(x0, x1, 5):
                red, green, blue = pixels[x, y]
                luminance = 0.299 * red + 0.587 * green + 0.114 * blue
                total += 1
                if luminance > 195:
                    bright += 1
        if total and bright / total > 0.08:
            lit += 1
    return lit


def detect_lighting_from_text(remark_html: str, title: str) -> Optional[int]:
    text = _clean(_unescape_remark(remark_html)) + " " + (title or "")
    if re.search(r"三面采?光|3面采?光|三向采?光", text):
        return 3
    if re.search(r"双面采?光|雙面采?光|两面采?光|2面采?光|雙向采?光", text):
        return 2
    if re.search(r"单面采?光|單面采?光|1面采?光", text):
        return 1
    return None


def detect_lighting_from_images(image_urls: list[str]) -> Optional[tuple[int, int]]:
    if Image is None or not image_urls:
        return None
    scores: list[int] = []
    for url in image_urls[:12]:
        img = _download_image(url)
        if img is None:
            continue
        bands = _lit_bands_in_image(img)
        if bands > 0:
            scores.append(bands)
    if not scores:
        return None
    counts = Counter(scores)
    mode = counts.most_common(1)[0][0]
    if mode >= 3 and counts.get(2, 0) >= max(2, len(scores) // 4):
        estimate = 2
    else:
        estimate = min(mode, 3)
    return estimate, len(scores)


def analyze_lighting_faces(remark_html: str, title: str, image_urls: list[str]) -> str:
    text_faces = detect_lighting_from_text(remark_html, title)
    if text_faces:
        return f"{text_faces} 面（依物件文字描述）"

    image_result = detect_lighting_from_images(image_urls)
    if image_result:
        faces, analyzed = image_result
        return f"{faces} 面（依 {analyzed} 張照片推估）"

    if not image_urls:
        return f"{UNDETERMINED}（無法取得房屋照片）"
    if Image is None:
        return f"{UNDETERMINED}（需安裝 Pillow 套件才能分析照片）"
    return f"{UNDETERMINED}（照片未見明顯窗戶特徵，建議現場確認）"


def _lighting_face_count(lighting_text: str) -> Optional[int]:
    match = re.search(r"(\d+)\s*面", lighting_text or "")
    return int(match.group(1)) if match else None


def _unescape_remark(remark: str) -> str:
    if not remark:
        return ""
    text = remark.replace("&nbsp;", " ")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&amp;", "&")
    return text


def _ratio_value(text: str) -> Optional[float]:
    if not text:
        return None
    match = re.search(r"([\d.]+)\s*%", text)
    return float(match.group(1)) if match else None


def _meters_value(text: str) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"約\s*(\d+)\s*公尺", text)
    return int(match.group(1)) if match else None


def build_pros_cons(
    house_age: str,
    public_ratio: str,
    nearest_mrt: str,
    nearest_supermarket: str,
    registered_ping: Optional[float],
    main_ping: Optional[float],
    parking_ping: Optional[float],
    floor: str,
    management_fee: str,
    bathroom_window: str,
    lighting_faces: str,
    market_comment: str,
    household_count: str,
) -> tuple[list[str], list[str]]:
    pros: list[str] = []
    cons: list[str] = []

    age_value = _to_float(house_age)
    if age_value is not None:
        if age_value <= 10:
            pros.append(f"屋齡僅 {age_value:.0f} 年，屋況與公設較新，短期整修成本低。")
        elif age_value >= 35:
            cons.append(f"屋齡約 {age_value:.0f} 年，需留意管線、防水與結構維護成本。")

    ratio = _ratio_value(public_ratio)
    if ratio is not None:
        if ratio <= 30:
            pros.append(f"公設比約 {ratio:.0f}%，實際可用空間比例較高。")
        elif ratio >= 35:
            cons.append(f"公設比約 {ratio:.0f}% 偏高，等於花錢買公共空間，實坪偏少。")

    mrt_m = _meters_value(nearest_mrt)
    if mrt_m is not None:
        if mrt_m <= 800:
            pros.append(f"最近捷運站約 {mrt_m} 公尺，通勤便利、對後續轉手有幫助。")
        elif mrt_m >= 1200:
            cons.append(f"最近捷運站約 {mrt_m} 公尺，步行較遠，需仰賴公車或自備交通工具。")

    shop_m = _meters_value(nearest_supermarket)
    if shop_m is not None and shop_m <= 600:
        pros.append(f"最近超市約 {shop_m} 公尺，日常採買方便。")
    elif shop_m is not None and shop_m >= 1200:
        cons.append(f"最近超市約 {shop_m} 公尺，採買需要開車或騎車。")

    if registered_ping and main_ping and registered_ping > 0:
        usable = registered_ping - (parking_ping or 0)
        if usable > 0:
            main_pct = main_ping / usable * 100
            if main_pct >= 60:
                pros.append(f"主建物約佔扣除車位後權狀的 {main_pct:.0f}%，室內實際空間紮實。")
            elif main_pct <= 55:
                cons.append(
                    f"主建物僅約 {main_ping:.2f} 坪，佔扣除車位後權狀的 {main_pct:.0f}%，室內坪效偏低。"
                )

    if parking_ping and parking_ping >= 5:
        pros.append(f"含車位約 {parking_ping} 坪，都會區稀缺、保值性較佳。")
    elif parking_ping in (None, 0):
        cons.append("未含車位，若有停車需求需額外承租或購買。")

    floor_match = re.match(r"(\d+)F/(\d+)F", floor or "")
    if floor_match:
        current, total = int(floor_match.group(1)), int(floor_match.group(2))
        if current == total:
            cons.append(f"位於頂樓（{current}F/{total}F），須留意西曬與屋頂防水。")
        elif current == 1:
            cons.append("位於一樓，須留意隱私、潮濕與噪音問題。")

    fee = _to_float(management_fee)
    if fee and fee >= 3000:
        cons.append(f"管理費約 {fee:.0f} 元/月，長期持有成本較高。")

    if bathroom_window.startswith("有對外窗"):
        pros.append("衛浴有對外窗，通風乾燥、較好維護。")
    elif bathroom_window.startswith("無對外窗"):
        cons.append("衛浴無對外窗，需仰賴排風設備，易有潮濕問題。")

    faces = _lighting_face_count(lighting_faces)
    if faces is not None:
        if faces >= 3:
            pros.append(f"約 {faces} 面採光，自然光充足、通風對流佳。")
        elif faces == 2:
            pros.append(f"約 {faces} 面採光，室內明亮度通常優於單面採光。")
        elif faces == 1:
            cons.append("僅約 1 面採光，另一側可能較暗，需留意通風與照明。")
    elif lighting_faces.startswith(UNDETERMINED):
        cons.append("採光面數無法由照片確認，現場看屋時建議留意各房間明暗與開窗位置。")

    households = _to_float(household_count)
    if households:
        if households <= 120:
            pros.append(f"社區約 {households:.0f} 戶，單純度較高、管理相對容易。")
        elif households >= 400:
            cons.append(f"社區約 {households:.0f} 戶，戶數多、人員較複雜。")

    if not pros:
        pros.append("資料不足，暫無法歸納明確優點。")
    if not cons:
        cons.append("資料不足，暫無法歸納明確缺點。")
    return pros, cons


def estimate_parking_price(deals: list[Deal]) -> tuple[Optional[int], Optional[int], str]:
    values: list[int] = []

    for deal in deals:
        if deal.special or not deal.total_wan or not deal.unit_price or not deal.area_ping:
            continue
        has_parking = deal.parking and "車位" in deal.parking and "無車位" not in deal.parking
        if not has_parking:
            continue
        implied = deal.total_wan - deal.unit_price * deal.area_ping
        if 80 <= implied <= 900:
            values.append(round(implied))

    no_parking = [
        d
        for d in deals
        if d.parking
        and "無車位" in d.parking
        and d.total_wan
        and d.area_ping
        and not d.special
    ]
    with_parking = [
        d
        for d in deals
        if d.parking
        and "車位" in d.parking
        and "無車位" not in d.parking
        and d.total_wan
        and d.area_ping
        and not d.special
    ]
    for wp in with_parking:
        for np in no_parking:
            if abs((wp.area_ping or 0) - (np.area_ping or 0)) > 4:
                continue
            if wp.unit_price and np.unit_price and abs(wp.unit_price - np.unit_price) > 15:
                continue
            diff = (wp.total_wan or 0) - (np.total_wan or 0)
            if 80 <= diff <= 900:
                values.append(round(diff))

    if not values:
        return None, None, ""

    values.sort()
    if len(values) >= 4:
        low = values[len(values) // 4]
        high = values[(3 * len(values)) // 4]
    else:
        low, high = values[0], values[-1]
    note = f"依同社區 {len(values)} 筆含車位成交推估"
    return low, high, note


def fetch_community_detail(community_id: str) -> dict:
    try:
        raw = _fetch(
            f"https://api.591.com.tw/tw/v1/community/detail?id={community_id}",
            accept="application/json",
        )
        data = json.loads(raw)
        community = data.get("data", {}).get("community") or {}
        return community if isinstance(community, dict) else {}
    except Exception:
        return {}


def estimate_market(
    registered_ping: Optional[float],
    parking_ping: Optional[float],
    community_avg: str,
    deals: list[Deal],
    ask_unit_price_str: str = "",
) -> tuple[str, str, str]:
    comparable = []
    for deal in deals:
        if deal.special:
            continue
        if deal.parking and "車位" in deal.parking and "無車位" not in deal.parking:
            continue
        if deal.area_ping is None or deal.unit_price is None:
            continue
        if deal.area_ping < 8 or deal.area_ping > 40:
            continue
        if deal.unit_price < 20 or deal.unit_price > 200:
            continue
        comparable.append(deal)

    if not comparable and deals:
        comparable = [d for d in deals if d.unit_price and not d.special][:12]

    unit_prices = [d.unit_price for d in comparable if d.unit_price]
    unit_prices.sort()
    median_unit = None
    estimate_source = "deals"
    if unit_prices:
        mid = len(unit_prices) // 2
        if len(unit_prices) % 2:
            median_unit = unit_prices[mid]
        else:
            median_unit = round((unit_prices[mid - 1] + unit_prices[mid]) / 2, 1)

    if not median_unit:
        avg_val = _parse_unit_price(community_avg)
        if avg_val:
            median_unit = avg_val
            unit_prices = [round(avg_val * 0.93, 1), avg_val, round(avg_val * 1.07, 1)]
            estimate_source = "community_avg"

    if not median_unit:
        list_val = _parse_unit_price(ask_unit_price_str)
        if list_val:
            median_unit = list_val
            unit_prices = [round(list_val * 0.95, 1), list_val, round(list_val * 1.05, 1)]
            estimate_source = "listing"

    avg_text = community_avg or UNKNOWN
    if median_unit and estimate_source == "deals":
        if avg_text == UNKNOWN:
            avg_text = f"近一年相似成交中位數約 {median_unit} 萬/坪"
        else:
            avg_text += f"；相似成交中位數約 {median_unit} 萬/坪"

    house_ping = None
    if registered_ping:
        house_ping = registered_ping - (parking_ping or 0)
        if house_ping <= 0:
            house_ping = registered_ping

    unit_price_range = UNKNOWN
    total_price_range = UNKNOWN
    comment = "無法取得社區成交或均價，且本戶亦無單價資料，暫無法估價。"
    if median_unit and house_ping:
        low_unit = min(unit_prices) if unit_prices else round(median_unit * 0.9, 1)
        high_unit = max(unit_prices) if unit_prices else round(median_unit * 1.1, 1)
        unit_price_range = f"{low_unit}～{high_unit} 萬/坪"

        house_low = round(low_unit * house_ping)
        house_high = round(high_unit * house_ping)
        has_parking = bool(parking_ping and parking_ping > 0)
        if has_parking:
            parking_low, parking_high, parking_source = estimate_parking_price(deals)
            if parking_low is None or parking_high is None:
                parking_low, parking_high = 280, 400
                parking_source = "同社區車位成交不足，改依區域行情估計"
            total_low = house_low + parking_low
            total_high = house_high + parking_high
            if parking_low == parking_high:
                parking_text = f"{parking_low} 萬"
            else:
                parking_text = f"{parking_low}～{parking_high} 萬"
            total_price_range = f"{total_low}～{total_high} 萬（包含車位 {parking_text}）"
            parking_note = f"{parking_source}，本戶車位約 {parking_ping:.1f} 坪。"
        else:
            total_price_range = f"{house_low}～{house_high} 萬"
            parking_note = "本戶無車位，總價僅含房屋本身。"

        if estimate_source == "deals" and comparable:
            comment = (
                f"依同社區近年相似成交（{min(d.area_ping for d in comparable):.1f}～"
                f"{max(d.area_ping for d in comparable):.1f} 坪）推估，"
                f"不含車位坪數約 {house_ping:.2f} 坪。{parking_note}"
            )
            if avg_text != UNKNOWN:
                comment = f"{avg_text}。{comment}"
        elif estimate_source == "community_avg":
            comment = (
                f"成交明細不足，改以社區均價 {median_unit} 萬/坪估算，"
                f"不含車位坪數約 {house_ping:.2f} 坪。{parking_note}"
            )
        else:
            comment = (
                f"無法取得社區成交，改以刊登單價 {median_unit} 萬/坪粗估，"
                f"不含車位坪數約 {house_ping:.2f} 坪。{parking_note}"
            )
    elif not house_ping:
        comment = "權狀坪數資料不足，暫無法估價。"
    return unit_price_range, total_price_range, comment


def _roc_year_month(text: str) -> Optional[tuple[int, int]]:
    match = re.search(r"(\d{2,3})[-/](\d{1,2})", text or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _deal_newer_than_official(date_text: str) -> bool:
    """591 成交年月是否晚於目前已公布的實價登錄季別。"""
    parsed = _roc_year_month(date_text)
    if not parsed:
        return False
    latest = plvr.recent_seasons()[0]
    year, quarter = int(latest[:3]), int(latest[-1])
    return parsed > (year, quarter * 3)


def _first_int(text: str) -> Optional[int]:
    match = re.search(r"\d+", _clean(text or ""))
    return int(match.group(0)) if match else None


def parse_listing_floor(text: str) -> Optional[int]:
    """解析 591 樓層欄，例如 3F/5F、B1、頂樓加蓋。回傳本戶所在層，地下為負。"""
    text = _clean(text or "")
    if not text or text == UNKNOWN:
        return None
    if re.search(r"頂樓加蓋|加蓋|整棟|全層|見使用層", text):
        return None
    if re.search(r"B\s*1|地下\s*一層|地下\s*1", text, re.I):
        return -1
    if re.search(r"B\s*2|地下\s*二層|地下\s*2", text, re.I):
        return -2
    match = re.search(r"(\d+)\s*(?:F|樓|層)", text, re.I)
    if match:
        return int(match.group(1))
    return _first_int(text)


def _plvr_parking_keywords(registered_type: str, parking_desc: str) -> tuple[str, ...]:
    """把 591 的車位說法轉成實價登錄的車位類別。

    591 寫「平面式」，實價登錄則細分成坡道平面／升降平面／一樓平面，字面對不上，
    因此改以類別群組比對；若實價登錄已載明本戶車位類別，優先直接採用。
    """
    if registered_type:
        return (registered_type,)

    desc = _clean(parking_desc)
    if "坡道平面" in desc:
        return ("坡道平面",)
    if "塔式" in desc:
        return ("塔式車位",)
    if "機械" in desc:
        return ("坡道機械", "升降機械")
    if "升降" in desc:
        return ("升降平面", "升降機械")
    if "平面" in desc:
        return ("坡道平面", "升降平面", "一樓平面")
    return ()


@dataclass
class MarketEstimate:
    unit_price_range: Optional[str] = None
    total_price_range: Optional[str] = None
    comment: Optional[str] = None
    previous_sale: str = UNKNOWN
    building_deals: list = field(default_factory=list)
    building_scope: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.unit_price_range)


def estimate_market_from_plvr(
    want: dict,
    door_numbers: set[int],
    registered_ping: Optional[float],
    parking_ping: Optional[float],
    parking_desc: str,
    building_floors: str,
    house_age: str,
    floor_text: str,
    sources: list[str],
) -> MarketEstimate:
    """以內政部實價登錄推估行情。估不出來時 unit_price_range 為 None，交由 591 的估算接手。"""
    if not want.get("city"):
        return MarketEstimate()

    try:
        deals, seasons = plvr.load_city_deals(want["city"])
    except Exception:
        return MarketEstimate()
    if not deals:
        return MarketEstimate()

    total_floor = _first_int(building_floors)
    age = _first_int(house_age)
    build_year = None
    if age is not None:
        build_year = (date.today().year - 1911) - age

    building_deals, building_scope = plvr.find_building_deals(
        deals, want.get("district", ""), want.get("road", ""), door_numbers, total_floor
    )
    if building_deals:
        sources.append(f"實價登錄同棟成交 {len(building_deals)} 筆（比對方式：{building_scope}）")

    previous, certain = plvr.find_previous_sale(
        building_deals, registered_ping, parking_ping, parse_listing_floor(floor_text)
    )
    previous_text = UNKNOWN
    if previous:
        build_year = previous.build_year or build_year
        note = f"；登錄備註：{previous.note.rstrip('；')}" if previous.note else ""
        prefix = "" if certain else "疑似（同棟同坪數，樓層無法確認）："
        previous_text = (
            f"{prefix}{previous.date} 以 {previous.total_wan:.0f} 萬成交"
            f"（{previous.address}，權狀 {previous.total_ping:.2f} 坪，"
            f"車位 {previous.parking_ping:.2f} 坪／{previous.parking_type or '未載明'}）{note}"
        )

    comparables, tier = plvr.find_comparables(
        deals,
        want.get("district", ""),
        building_deals,
        want.get("road", ""),
        total_floor=total_floor,
        build_year=build_year,
        building_type=previous.building_type if previous else "",
    )
    result = MarketEstimate(
        previous_sale=previous_text,
        building_deals=sorted(building_deals, key=lambda d: d.date, reverse=True),
        building_scope=building_scope,
    )

    low_unit, mid_unit, high_unit, sample = plvr.unit_price_quartiles(comparables)
    if not low_unit or not high_unit:
        return result

    house_ping = None
    if registered_ping:
        house_ping = registered_ping - (parking_ping or 0)
        if house_ping <= 0:
            house_ping = registered_ping

    unit_range = f"{low_unit}～{high_unit} 萬/坪"
    parking_note = "本戶無車位，總價僅含房屋本身。" if not parking_ping else ""
    total_low = round(low_unit * house_ping) if house_ping else None
    total_high = round(high_unit * house_ping) if house_ping else None

    if parking_ping and parking_ping > 0:
        park_low, park_high, _, park_note = plvr.parking_price_range(
            deals,
            want.get("district", ""),
            _plvr_parking_keywords(previous.parking_type if previous else "", parking_desc),
        )
        if park_low and park_high:
            if total_low is not None and total_high is not None:
                total_low += park_low
                total_high += park_high
            span = f"{park_low} 萬" if park_low == park_high else f"{park_low}～{park_high} 萬"
            parking_note = f"車位約 {parking_ping:.2f} 坪，估 {span}（{park_note}）。"
        else:
            parking_note = f"車位約 {parking_ping:.2f} 坪，但查無同區車位申報價，總價未計入車位。"

    result.unit_price_range = unit_range
    result.total_price_range = (
        f"{total_low}～{total_high} 萬" if total_low is not None and total_high is not None else None
    )
    ping_note = f"本戶扣除車位後約 {house_ping:.2f} 坪。" if house_ping else "本戶權狀坪數未知，僅提供單價區間。"
    result.comment = (
        f"依內政部實價登錄近 {len(seasons)} 季資料，取「{tier}」共 {sample} 筆成交，"
        f"房屋單價中位 {mid_unit} 萬/坪；{ping_note}{parking_note}"
    )
    sources.append(
        f"內政部實價登錄開放資料（{seasons[-1]}～{seasons[0]}，{want['city']}買賣 {len(deals)} 筆）"
    )
    sources.append(f"行情比對範圍：{tier}（{sample} 筆）")
    return result


def _extract_parking_type(parking_desc: str) -> str:
    if not parking_desc:
        return ""
    for keyword in ("坡道平面", "平面式", "機械式", "升降式", "立體式", "塔式", "地下室"):
        if keyword in parking_desc:
            return keyword
    match = re.search(r"坪[，,]\s*([^，,]+)", parking_desc)
    if match:
        candidate = match.group(1).strip()
        if candidate and not re.search(r"含|售金|價", candidate) and len(candidate) <= 12:
            return candidate
    return ""


def _format_parking_status(has_parking: bool, parking_desc: str) -> str:
    if not has_parking:
        return "無"
    parking_type = _extract_parking_type(parking_desc)
    if parking_type:
        return f"有（{parking_type}）"
    return "有"


def _parse_parking_info(areas: dict, base: dict) -> tuple[Optional[float], str]:
    parking_raw = _clean(areas.get("車位面積") or "")
    parking_desc = _clean(base.get("parking") or "")

    if "無車位" in parking_desc:
        return None, "無"

    parking_ping = None
    if parking_raw not in ("-", "—", "0", "0坪", "無", ""):
        parking_ping = _to_float(parking_raw)
    if not parking_ping and parking_desc:
        match = re.search(r"([\d.]+)\s*坪", parking_desc)
        if match:
            parking_ping = _to_float(match.group(1))

    if parking_ping and parking_ping > 0:
        return parking_ping, _format_parking_status(True, parking_desc)

    if parking_desc and "車位" in parking_desc and re.search(r"[\d.]+\s*坪", parking_desc):
        return parking_ping, _format_parking_status(True, parking_desc)

    if parking_raw in ("-", "—", "0", "0坪", "無"):
        return None, "無"

    return None, "無"


_LEJU_OPENER = None


def _leju_headers(*, html: bool = False) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.leju.com.tw/",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if html:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        headers["Upgrade-Insecure-Requests"] = "1"
    else:
        headers["Accept"] = "application/json, text/plain, */*"
        headers["Origin"] = "https://www.leju.com.tw"
    return headers


def _leju_opener():
    global _LEJU_OPENER
    if _LEJU_OPENER is not None:
        return _LEJU_OPENER
    jar = CookieJar()
    _LEJU_OPENER = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=_ssl_context()),
    )
    try:
        _LEJU_OPENER.open(
            urllib.request.Request("https://www.leju.com.tw/", headers=_leju_headers(html=True)),
            timeout=20,
        ).read()
    except Exception:
        pass
    return _LEJU_OPENER


def _fetch_leju_html(url: str) -> str:
    req = urllib.request.Request(url, headers=_leju_headers(html=True))
    with _leju_opener().open(req, timeout=25) as response:
        return response.read().decode("utf-8", "ignore")


def parse_leju_community_html(html: str, community_id: str = "") -> dict:
    result = {
        "name": "",
        "land_area": "",
        "household_count": "",
        "building_floors": "",
        "public_ratio": "",
        "address": "",
        "community_id": community_id,
        "url": f"https://www.leju.com.tw/community/{community_id}" if community_id else "",
    }
    title = re.search(r"<title>【([^】]+)】", html)
    if title:
        result["name"] = html_module.unescape(title.group(1)).split("/")[0].strip()
    desc = re.search(r'<meta name="description" content="([^"]+)"', html)
    if desc:
        match = re.search(r"位於([^，,]+)", html_module.unescape(desc.group(1)))
        if match:
            result["address"] = match.group(1).strip()

    for match in re.finditer(
        r"<dt[^>]*>.*?<p[^>]*>([^<]+)</p>.*?<dd[^>]*>.*?<!--\[-->([^<]+?)<!--\]-->",
        html,
        re.S,
    ):
        label = _clean(match.group(1))
        value = html_module.unescape(_clean(match.group(2)))
        if label == "地址" and value:
            result["address"] = value
        elif label == "總戶數" and value:
            result["household_count"] = value
        elif label == "總樓高" and value:
            result["building_floors"] = value
        elif label == "公設比" and value:
            result["public_ratio"] = value.replace("％", "%")
        elif label == "基地面積" and value:
            result["land_area"] = value if "坪" in value else f"{value} 坪"
    return result


LEJU_CITY_CODES = {
    "台北市": "A", "台中市": "B", "基隆市": "C", "台南市": "D", "高雄市": "E",
    "新北市": "F", "宜蘭縣": "G", "桃園市": "H", "嘉義市": "I", "新竹縣": "J",
    "苗栗縣": "K", "南投縣": "M", "彰化縣": "N", "新竹市": "O", "雲林縣": "P",
    "嘉義縣": "Q", "屏東縣": "T", "花蓮縣": "U", "台東縣": "V", "金門縣": "W",
    "澎湖縣": "X", "連江縣": "Z",
}

_LEJU_ROAD_RE = re.compile(r"([\u4e00-\u9fff\w]{1,10}?(?:大道|路|街|巷)(?:[一二三四五六七八九十]段)?)")


def _leju_norm(text: str) -> str:
    """去掉空白、標點與全形差異，方便社區名比對。"""
    text = re.sub(r"<[^>]+>", "", _clean(text or ""))
    text = html_module.unescape(text)
    text = text.replace("臺", "台").replace("・", "").replace("·", "")
    return re.sub(r"[\s\-－—_/／、,，.．()（）]", "", text).lower()


def _split_tw_address(*addresses: str) -> dict:
    text = " ".join(_clean(a) for a in addresses if a and a != UNKNOWN)
    text = text.replace("臺", "台")
    city = next((c for c in LEJU_CITY_CODES if c in text), "")
    district = ""
    match = re.search(r"[\u4e00-\u9fff]{1,3}[區鄉鎮]|[\u4e00-\u9fff]{2}市(?![市縣])", text[text.find(city) + len(city):] if city else text)
    if match:
        district = match.group(0)
    tail = text.split(district, 1)[1] if district and district in text else text
    road_match = _LEJU_ROAD_RE.search(tail)
    number = re.search(r"(\d+)\s*號", tail)
    return {
        "text": text,
        "city": city,
        "district": district,
        "road": road_match.group(1) if road_match else "",
        "number": number.group(1) if number else "",
    }


def _leju_api_json(path: str, params: dict) -> dict:
    url = "https://api.leju.com.tw/api/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_leju_headers())
    with _leju_opener().open(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8", "ignore"))


def search_leju_communities(keyword: str, city_code: str = "", building_type: int = 0) -> list[dict]:
    """呼叫樂居站內搜尋，回傳社區候選（含縣市、行政區、社區 ID）。"""
    if not keyword:
        return []
    try:
        payload = _leju_api_json(
            "search/all",
            {
                "city": city_code,
                "keyword": keyword,
                "page": 1,
                "page_limit": 20,
                "type": 5,
                "building_type": building_type,
            },
        )
    except Exception:
        return []

    results = []
    for item in payload.get("data") or []:
        community_id = _clean(str(item.get("tag_id") or ""))
        if not community_id.startswith("L"):
            continue
        results.append(
            {
                "id": community_id,
                "name": html_module.unescape(re.sub(r"<[^>]+>", "", item.get("text") or "")).strip(),
                "city": _clean(item.get("city") or ""),
                "district": _clean(item.get("area") or ""),
            }
        )
    return results


def _pick_leju_candidate(candidates: list[dict], want: dict, community_name: str) -> Optional[dict]:
    """先要求同縣市同行政區，再看社區名相似度，避免抓到同名的外區社區。"""
    target_name = _leju_norm(community_name)
    scored = []
    for item in candidates:
        if want["city"] and item["city"] and _leju_norm(item["city"]) != _leju_norm(want["city"]):
            continue
        if want["district"] and item["district"] and _leju_norm(item["district"]) != _leju_norm(want["district"]):
            continue
        item_name = _leju_norm(item["name"])
        if target_name and item_name:
            if item_name == target_name:
                score = 3
            elif target_name in item_name or item_name in target_name:
                score = 2
            else:
                score = 1
        else:
            score = 1
        scored.append((score, item))
    if not scored:
        return None
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[0][1]


_LEJU_COMMUNITY_CACHE: dict = {}


def fetch_leju_community(community_name: str, community_address: str, listing_address: str = "") -> dict:
    want = _split_tw_address(community_address, listing_address)
    city_code = LEJU_CITY_CODES.get(want["city"], "")
    cache_key = (_leju_norm(community_name), want["city"], want["district"], want["road"])
    if cache_key in _LEJU_COMMUNITY_CACHE:
        return _LEJU_COMMUNITY_CACHE[cache_key]

    keywords = [k for k in (community_name, want["road"]) if k]
    candidate = None
    for keyword in keywords:
        candidate = _pick_leju_candidate(
            search_leju_communities(keyword, city_code), want, community_name
        )
        if candidate:
            break

    result: dict = {}
    if candidate:
        try:
            html = _fetch_leju_html(f"https://www.leju.com.tw/community/{candidate['id']}")
            parsed = parse_leju_community_html(html, candidate["id"])
            if _leju_result_matches(parsed, candidate, want, community_name):
                parsed["address"] = _leju_full_address(parsed.get("address", ""), candidate)
                result = parsed
        except Exception:
            result = {}

    _LEJU_COMMUNITY_CACHE[cache_key] = result
    return result


def _leju_result_matches(parsed: dict, candidate: dict, want: dict, community_name: str) -> bool:
    """社區名一致就採用；名稱對不上時，至少要同一條路。"""
    if _leju_norm(parsed.get("name", "")) and _leju_norm(community_name):
        if _leju_norm(community_name) in _leju_norm(parsed["name"]) or _leju_norm(parsed["name"]) in _leju_norm(community_name):
            return True
    if want["road"] and want["road"] in _clean(parsed.get("address", "")):
        return True
    return False


def _leju_full_address(address: str, candidate: dict) -> str:
    address = html_module.unescape(_clean(address))
    if not address:
        return ""
    prefix = ""
    if candidate.get("city") and candidate["city"] not in address:
        prefix += candidate["city"]
    if candidate.get("district") and candidate["district"] not in address:
        prefix += candidate["district"]
    return prefix + address


COMMUNITY_FIELD_LABELS = {
    "community_name": "社區名稱",
    "land_area": "基地面積",
    "household_count": "總戶數",
    "building_floors": "總樓高",
    "public_ratio": "公設比",
    "community_address": "社區地址",
}

_LEJU_FIELD_KEYS = {
    "community_name": "name",
    "land_area": "land_area",
    "household_count": "household_count",
    "building_floors": "building_floors",
    "public_ratio": "public_ratio",
    "community_address": "address",
}


def merge_community_fields(fields_591: dict, leju: dict, sources: list[str]) -> dict:
    """社區環境欄位以樂居為主，樂居沒寫的才用 591 補。"""
    merged = {}
    from_leju, from_591, missing = [], [], []

    for key, label in COMMUNITY_FIELD_LABELS.items():
        leju_value = _clean((leju or {}).get(_LEJU_FIELD_KEYS[key]) or "")
        value_591 = _clean(fields_591.get(key) or "")
        if value_591 == UNKNOWN:
            value_591 = ""

        if leju_value:
            merged[key] = leju_value
            from_leju.append(label)
            if value_591 and value_591 != leju_value:
                from_leju[-1] = f"{label}（591 為 {value_591}）"
        elif value_591:
            merged[key] = value_591
            from_591.append(label)
        else:
            merged[key] = UNKNOWN
            missing.append(label)

    if leju and leju.get("url"):
        sources.append(f"樂居社區頁：{leju['url']}")
    else:
        sources.append("樂居：未找到符合地址的社區頁，社區環境改採 591")

    if from_leju:
        sources.append("社區環境採樂居：" + "、".join(from_leju))
    if from_591:
        sources.append("社區環境採 591（樂居未提供）：" + "、".join(from_591))
    if missing:
        sources.append("社區環境兩站皆無：" + "、".join(missing))

    return merged


def _format_registered_area(registered: str, parking_ping: Optional[float]) -> str:
    ping = _to_float(registered)
    if ping is None:
        text = _clean(registered or "")
        return text if text else UNKNOWN
    if parking_ping and parking_ping > 0:
        return f"{ping:.2f}坪（內含車位 {parking_ping:.2f}坪）"
    return f"{ping:.2f}坪（無車位）"


def _build_listing_address(base: dict) -> str:
    addr = base.get("address") or {}
    parts = [
        addr.get("region", ""),
        addr.get("section", ""),
        addr.get("street", ""),
        f"{addr.get('alley')}巷" if addr.get("alley") else "",
        f"{addr.get('lane')}弄" if addr.get("lane") else "",
    ]
    if addr.get("addr_number") and not addr.get("hide_addr_detail"):
        parts.append(f"{addr.get('addr_number')}號")
    text = "".join(parts)
    if not text:
        return UNKNOWN
    if addr.get("hide_addr_detail"):
        return f"{text}（門牌未公開，地圖依刊登座標）"
    return text


def _build_address(base: dict, community_address: str) -> str:
    if community_address:
        return community_address
    return _build_listing_address(base)


def analyze_591(url: str) -> AnalysisReport:
    clean_url = url.strip().split("?")[0]
    house_id = _parse_591_id(clean_url)
    detail = fetch_591_detail(house_id)
    base = detail.get("baseInfo") or {}
    info = _info_map(base.get("info"))
    areas = _info_map(base.get("areaIntro"))
    gtm = detail.get("gtm_detail_data") or {}

    community_id = fetch_community_id(house_id)
    community_html = ""
    community = {}
    community_detail = {}
    sources = [f"591 物件頁：{clean_url}"]
    if community_id:
        community_url = f"https://market.591.com.tw/{community_id}"
        community_html = _fetch(community_url)
        community = parse_community_page(community_html)
        community_detail = fetch_community_detail(community_id)
        sources.append(f"591 實價登錄社區頁：{community_url}")
    listing_address = _build_listing_address(base)
    community_address = _build_address(base, community.get("address", ""))

    listing_lat = _to_float((base.get("address") or {}).get("lat"))
    listing_lon = _to_float((base.get("address") or {}).get("lng"))
    community_lat = _to_float(community_detail.get("lat"))
    community_lon = _to_float(community_detail.get("lng"))

    api_key = _google_maps_api_key()
    nearby_lat = community_lat or listing_lat
    nearby_lon = community_lon or listing_lon
    if (not nearby_lat or not nearby_lon) and api_key:
        nearby_lat, nearby_lon = geocode_google(community_address, api_key)
    nearest_mrt, nearest_shop = UNKNOWN, UNKNOWN
    if nearby_lat and nearby_lon:
        nearest_mrt, nearest_shop = fetch_nearby_cached(nearby_lat, nearby_lon, api_key=api_key)
    if nearest_mrt == UNKNOWN or nearest_shop == UNKNOWN:
        fb_mrt, fb_shop = _fallback_nearby_from_591(detail.get("nearInfo") or {})
        if nearest_mrt == UNKNOWN:
            nearest_mrt = fb_mrt
        if nearest_shop == UNKNOWN:
            nearest_shop = fb_shop

    if api_key and nearby_lat and nearby_lon:
        sources.append("Google Maps 周邊設施（步行距離）")
    else:
        sources.append("OpenStreetMap 周邊設施")

    registered = areas.get("登記總面積") or base.get("unitArea") or ""
    main_area = areas.get("主建物") or base.get("mainArea") or ""
    parking_area, parking_status = _parse_parking_info(areas, base)
    registered_ping = _to_float(registered)
    ask_price = _to_float(base.get("price"))

    public_ratio = community.get("public_ratio") or info.get("公設比") or UNKNOWN
    house_ratio = info.get("公設比")
    if house_ratio and public_ratio not in (UNKNOWN, house_ratio):
        public_ratio = f"{public_ratio}（本戶刊登 {house_ratio}）"

    house_age = _or_unknown(
        info.get("屋齡") or (f"{gtm.get('house_age_name')}年" if gtm.get("house_age_name") else "")
    )
    floor_text = _or_unknown(info.get("樓層"))

    listing_address = _build_listing_address(base)
    community_address = _build_address(base, community.get("address", ""))

    leju = fetch_leju_community(
        community.get("name") or _clean(base.get("communityName") or ""),
        community_address,
        listing_address,
    )

    fields_591 = {
        "community_name": community.get("name", ""),
        "land_area": community.get("land_area", ""),
        "household_count": community.get("household_count", ""),
        "building_floors": community.get("building_floors", ""),
        "public_ratio": public_ratio,
        "community_address": community_address,
    }
    report_fields = merge_community_fields(fields_591, leju, sources)

    want = _split_tw_address(report_fields["community_address"], community_address, listing_address)
    door_numbers = {
        number
        for addr in (
            report_fields["community_address"],
            community_address,
            listing_address,
            leju.get("address", ""),
        )
        if addr and addr != UNKNOWN
        for number in [plvr.door_number(addr)]
        if number
    }

    market = estimate_market_from_plvr(
        want,
        door_numbers,
        registered_ping,
        parking_area,
        _clean(base.get("parking") or ""),
        report_fields["building_floors"],
        house_age,
        floor_text,
        sources,
    )
    unit_range, total_range, comment = market.unit_price_range, market.total_price_range, market.comment
    previous_sale = market.previous_sale
    if not market.ok:
        unit_range, total_range, comment = estimate_market(
            registered_ping,
            parking_area,
            community.get("avg_unit", ""),
            community.get("deals", []),
            ask_unit_price_str=str(base.get("unitPrice") or info.get("單價") or ""),
        )
        sources.append("實價登錄比對不足，行情改以 591 社區成交估算")

    deals_591 = community.get("deals", [])[:12]
    fresh_591 = [d for d in deals_591 if _deal_newer_than_official(d.date)]
    if fresh_591 and comment:
        comment += (
            f" 591 社區頁另有 {len(fresh_591)} 筆晚於官方開放資料的成交，"
            "請一併參考下方「591 社區近期成交」。"
        )

    bathroom_window = detect_bathroom_window(detail.get("remark", ""), base.get("title", ""))
    image_urls = fetch_listing_images(house_id)
    community_images = parse_community_images(community_html) if community_html else []
    lighting_faces = analyze_lighting_faces(detail.get("remark", ""), base.get("title", ""), image_urls)

    pros, cons = build_pros_cons(
        house_age=house_age,
        public_ratio=public_ratio,
        nearest_mrt=nearest_mrt,
        nearest_supermarket=nearest_shop,
        registered_ping=registered_ping,
        main_ping=_to_float(main_area),
        parking_ping=parking_area,
        floor=floor_text,
        management_fee=info.get("管理費", ""),
        bathroom_window=bathroom_window,
        lighting_faces=lighting_faces,
        market_comment=comment,
        household_count=community.get("household_count", ""),
    )

    return AnalysisReport(
        title=_or_unknown(base.get("title")),
        listing_id=f"S{house_id}",
        source_url=clean_url,
        image_url="",
        ask_price_wan=ask_price,
        ask_unit_price=_or_unknown(base.get("unitPrice") or info.get("單價")),
        layout=_or_unknown(base.get("layout")),
        community_name=report_fields["community_name"],
        land_area=report_fields["land_area"],
        household_count=report_fields["household_count"],
        building_floors=report_fields["building_floors"],
        public_ratio=report_fields["public_ratio"],
        community_address=report_fields["community_address"],
        listing_address=_or_unknown(listing_address),
        nearest_mrt=_or_unknown(nearest_mrt),
        nearest_supermarket=_or_unknown(nearest_shop),
        registered_area=_format_registered_area(registered, parking_area),
        parking_status=parking_status,
        house_age=house_age,
        main_building_area=_or_unknown(main_area),
        registered_use=_or_unknown(info.get("用途")),
        floor=floor_text,
        bathroom_window=bathroom_window,
        lighting_faces=lighting_faces,
        unit_price_range=_or_unknown(unit_range),
        total_price_range=_or_unknown(total_range),
        market_comment=_or_unknown(comment),
        previous_sale=previous_sale,
        building_deals=market.building_deals,
        building_deal_scope=market.building_scope,
        interior_images=image_urls[:16],
        community_images=community_images,
        latitude=community_lat,
        longitude=community_lon,
        pros=pros,
        cons=cons,
        deals=deals_591,
        sources=sources,
    )


def analyze_leju_community_url(url: str) -> AnalysisReport:
    """直接貼樂居社區頁時，仍可產出社區環境與同棟實價登錄，但沒有單一戶的權狀與開價。"""
    match = re.search(r"/community/(L[A-Za-z0-9]+)", url)
    if not match:
        raise ValueError("這不是有效的樂居社區頁網址。分析單一戶請改貼 591 售屋網址。")
    community_id = match.group(1)
    html = _fetch_leju_html(f"https://www.leju.com.tw/community/{community_id}")
    leju = parse_leju_community_html(html, community_id)
    if not leju.get("name"):
        raise ValueError("樂居社區頁讀取失敗，請稍後再試，或改貼 591 售屋網址。")

    sources = [f"樂居社區頁：{leju.get('url') or url}"]
    report_fields = merge_community_fields(
        {
            "community_name": "",
            "land_area": "",
            "household_count": "",
            "building_floors": "",
            "public_ratio": "",
            "community_address": "",
        },
        leju,
        sources,
    )
    want = _split_tw_address(report_fields["community_address"], leju.get("address", ""))
    door_numbers = {n for n in [plvr.door_number(leju.get("address", ""))] if n}
    market = estimate_market_from_plvr(
        want,
        door_numbers,
        None,
        None,
        "",
        report_fields["building_floors"],
        "",
        "",
        sources,
    )
    return AnalysisReport(
        title=report_fields["community_name"],
        listing_id=community_id,
        source_url=leju.get("url") or url,
        image_url="",
        ask_price_wan=None,
        ask_unit_price=UNKNOWN,
        layout=UNKNOWN,
        community_name=report_fields["community_name"],
        land_area=report_fields["land_area"],
        household_count=report_fields["household_count"],
        building_floors=report_fields["building_floors"],
        public_ratio=report_fields["public_ratio"],
        community_address=report_fields["community_address"],
        listing_address=UNKNOWN,
        nearest_mrt=UNKNOWN,
        nearest_supermarket=UNKNOWN,
        registered_area=UNKNOWN,
        parking_status=UNKNOWN,
        house_age=UNKNOWN,
        main_building_area=UNKNOWN,
        registered_use=UNKNOWN,
        floor=UNKNOWN,
        bathroom_window=UNKNOWN,
        lighting_faces=UNKNOWN,
        unit_price_range=_or_unknown(market.unit_price_range),
        total_price_range=_or_unknown(market.total_price_range),
        market_comment=_or_unknown(market.comment) + " 這是社區頁，沒有單一戶開價與權狀，總價區間無法估算。",
        previous_sale=UNKNOWN,
        building_deals=market.building_deals,
        building_deal_scope=market.building_scope,
        sources=sources,
    )


def analyze_url(url: str) -> AnalysisReport:
    text = (url or "").strip()
    if re.fullmatch(r"\d{6,}", text):
        return analyze_591(text)
    host = urlparse(text).netloc.lower()
    if "591.com.tw" in host or (not host and "591" in text):
        return analyze_591(text)
    if "leju.com.tw" in host and "/community/" in text:
        return analyze_leju_community_url(text)
    if "rakuya.com.tw" in host:
        raise ValueError("樂屋網址尚不支援。請改貼 591 售屋網址（電腦版、手機版或物件編號都可以），系統會自動去樂居與實價登錄補資料。")
    raise ValueError("目前請貼上 591 售屋網址（電腦版、手機版或物件編號都可以）。樂居社區頁也可以，但沒有單一戶的開價與權狀。")
