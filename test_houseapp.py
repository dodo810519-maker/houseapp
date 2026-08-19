import unittest
from datetime import date

import plvr
from plvr import LandDeal
from scraper import (
    _deal_newer_than_official,
    _parse_591_id,
    _plvr_parking_keywords,
    merge_community_fields,
    parse_listing_floor,
)


def deal(**kwargs) -> LandDeal:
    defaults = dict(
        date="114-01",
        address="台北市南港區經貿一路59號五樓",
        floor="五層",
        floor_no=5,
        total_floor=9,
        total_wan=1598,
        parking_wan=0,
        total_ping=20.69,
        parking_ping=5.64,
        house_ping=15.05,
        house_unit_price=None,
        parking_type="升降平面",
        layout="1房1廳",
        building_type="華廈(10層含以下有電梯)",
        build_year=98,
        note="",
    )
    defaults.update(kwargs)
    return LandDeal(**defaults)


class AddressTests(unittest.TestCase):
    def test_normalize_and_door(self):
        text = plvr.normalize_address("臺北市南港區經貿一路５９號五樓")
        self.assertEqual(text, "台北市南港區經貿一路59號五樓")
        self.assertEqual(plvr.building_key(text), "台北市南港區經貿一路59號")
        self.assertEqual(plvr.door_number(text), 59)


class FloorParseTests(unittest.TestCase):
    def test_listing_floor(self):
        self.assertEqual(parse_listing_floor("3F/5F"), 3)
        self.assertEqual(parse_listing_floor("5F/9F"), 5)
        self.assertEqual(parse_listing_floor("B1/5F"), -1)
        self.assertIsNone(parse_listing_floor("頂樓加蓋"))
        self.assertIsNone(parse_listing_floor("整棟"))

    def test_plvr_floor(self):
        self.assertEqual(plvr._floor_to_int("五層"), 5)
        self.assertEqual(plvr._floor_to_int("十二層"), 12)
        self.assertEqual(plvr._floor_to_int("五層之2"), 5)
        self.assertEqual(plvr._floor_to_int("地下一層"), -1)
        self.assertIsNone(plvr._floor_to_int("全層"))
        self.assertIsNone(plvr._floor_to_int("一層，二層，三層"))


class PreviousSaleTests(unittest.TestCase):
    def test_same_ping_different_floor_is_not_this_unit(self):
        other = deal(
            address="台北市內湖區內湖路二段103巷92弄6號四樓之2",
            floor="四層",
            floor_no=4,
            total_ping=17.27,
            parking_ping=0,
            house_ping=17.27,
            total_wan=1688,
        )
        found, certain = plvr.find_previous_sale([other], 17.27, 0, floor_no=3)
        self.assertIsNone(found)
        self.assertFalse(certain)

    def test_same_floor_and_ping_is_this_unit(self):
        same = deal()
        found, certain = plvr.find_previous_sale([same], 20.69, 5.64, floor_no=5)
        self.assertIs(found, same)
        self.assertTrue(certain)

    def test_unknown_floor_is_uncertain(self):
        same = deal()
        found, certain = plvr.find_previous_sale([same], 20.69, 5.64, floor_no=None)
        self.assertIs(found, same)
        self.assertFalse(certain)


class ParkingKeywordTests(unittest.TestCase):
    def test_591_plain_maps_to_official_groups(self):
        self.assertEqual(
            _plvr_parking_keywords("", "1個平面式車位"),
            ("坡道平面", "升降平面", "一樓平面"),
        )

    def test_official_type_wins(self):
        self.assertEqual(_plvr_parking_keywords("升降平面", "平面式"), ("升降平面",))


class UrlParseTests(unittest.TestCase):
    def test_desktop_mobile_and_bare_id(self):
        self.assertEqual(_parse_591_id("https://sale.591.com.tw/home/house/detail/2/20676115.html?from=x"), "20676115")
        self.assertEqual(_parse_591_id("https://m.591.com.tw/v2/sale/detail/20266044"), "20266044")
        self.assertEqual(_parse_591_id("20676115"), "20676115")


class MergeCommunityTests(unittest.TestCase):
    def test_leju_overrides_591_and_591_fills_gap(self):
        sources = []
        merged = merge_community_fields(
            {
                "community_name": "YES新世貿",
                "land_area": "1703坪",
                "household_count": "55戶",
                "building_floors": "地上 9 層",
                "public_ratio": "35%",
                "community_address": "台北市南港區經貿一路59號",
            },
            {
                "name": "YES新世貿",
                "land_area": "",
                "household_count": "55 戶",
                "building_floors": "9 樓",
                "public_ratio": "35.27%",
                "address": "經貿一路55號",
                "url": "https://www.leju.com.tw/community/L1c4169353bc68",
            },
            sources,
        )
        self.assertEqual(merged["public_ratio"], "35.27%")
        self.assertEqual(merged["land_area"], "1703坪")
        self.assertTrue(any("樂居" in s and "公設比" in s for s in sources))
        self.assertTrue(any("591" in s and "基地面積" in s for s in sources))


class SeasonTests(unittest.TestCase):
    def test_recent_seasons_order(self):
        seasons = plvr.recent_seasons(4, today=date(2026, 8, 19))
        self.assertEqual(seasons, ["115S2", "115S1", "114S4", "114S3"])

    def test_fresh_591_deal_detection(self):
        self.assertTrue(_deal_newer_than_official("115-08"))
        self.assertFalse(_deal_newer_than_official("114-01"))


if __name__ == "__main__":
    unittest.main()
