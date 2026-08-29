import unittest
from datetime import date

import plvr
from plvr import LandDeal
from scraper import (
    _annotate_public_ratio,
    _community_belongs_to_listing,
    _community_id_from_html,
    _community_name_from_listing,
    _deal_newer_than_official,
    _format_registered_area,
    _parse_cthouse_id,
    _parse_591_id,
    _parse_etwarm_id,
    _parse_hbhousing_id,
    _parse_housefun_id,
    _parse_parking_info,
    _parse_sinyi_id,
    _parse_twhg_id,
    _parse_yungching_id,
    _leju_names_compatible,
    _leju_name_score,
    _leju_result_matches,
    _pick_leju_candidate,
    _plvr_parking_keywords,
    _price_to_wan,
    _split_tw_address,
    apply_rakuya_community,
    _google_translate_proxy_url,
    _rakuya_community_url,
    _rakuya_ehid,
    _rakuya_page_ready,
    merge_community_fields,
    parse_cthouse_listing,
    parse_etwarm_html,
    parse_hbhousing_html,
    parse_housefun_html,
    parse_listing_floor,
    parse_rakuya_html,
    parse_sinyi_html,
    parse_twhg_html,
    parse_yungching_html,
    PortalListing,
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

    def test_sub_number_and_lane_are_not_the_main_door(self):
        self.assertEqual(plvr.door_number("台北市內湖區康寧路三段54-5號"), 54)
        self.assertEqual(plvr.door_number("台北市內湖區康寧路三段54之5號七樓"), 54)
        self.assertEqual(plvr.parse_house_number("台北市內湖區康寧路三段54-5號"), (None, None, 54, 5))
        self.assertEqual(plvr.parse_house_number("台北市內湖區康寧路三段54巷12號"), (54, None, 12, None))
        self.assertEqual(plvr.parse_house_number("台北市內湖區康寧路三段189巷54號"), (189, None, 54, None))
        self.assertEqual(
            plvr.building_key("台北市內湖區康寧路三段54-5號七樓"),
            "台北市內湖區康寧路三段54之5號",
        )

    def test_building_cluster_ignores_same_number_on_another_lane(self):
        listing = ["台北市內湖區康寧路三段54-5號"]
        self.assertTrue(plvr._same_building_cluster("台北市內湖區康寧路三段54之5號七樓", listing))
        self.assertTrue(plvr._same_building_cluster("台北市內湖區康寧路三段54之6號", listing))
        self.assertTrue(plvr._same_building_cluster("台北市內湖區康寧路三段54巷12號", listing))
        self.assertFalse(plvr._same_building_cluster("台北市內湖區康寧路三段189巷54號", listing))

    def test_find_building_deals_does_not_use_whole_road(self):
        same = deal(
            address="台北市內湖區康寧路三段54之5號七樓",
            total_floor=13,
            house_unit_price=97.47,
            parking_ping=0,
            house_ping=24.19,
            building_type="住宅大樓(11層含以上有電梯)",
        )
        other_lane = deal(
            address="台北市內湖區康寧路三段189巷54號一樓",
            total_floor=6,
            house_unit_price=50.17,
            parking_ping=0,
            house_ping=40.26,
        )
        cheap_same_road = deal(
            address="台北市內湖區康寧路三段200號五樓",
            total_floor=10,
            house_unit_price=61.9,
            parking_ping=0,
            house_ping=20,
        )
        short = deal(
            address="台北市內湖區康寧路三段54巷9號二樓",
            total_floor=5,
            house_unit_price=81.27,
            parking_ping=0,
            house_ping=28.3,
        )
        found, scope = plvr.find_building_deals(
            [same, other_lane, cheap_same_road, short],
            "內湖區",
            "康寧路三段",
            {54},
            total_floor=10,
            addresses=["台北市內湖區康寧路三段54-5號"],
        )
        self.assertEqual(found, [same])
        self.assertEqual(scope, "同社區門牌")
        comparables, tier = plvr.find_comparables(
            [same, other_lane, cheap_same_road, short],
            "內湖區",
            found,
            "康寧路三段",
            total_floor=10,
            build_year=85,
        )
        self.assertEqual(comparables, [same])
        self.assertEqual(tier, "同棟成交")
        low, mid, high, n = plvr.unit_price_quartiles(
            [
                deal(house_unit_price=83.21, parking_ping=0),
                deal(house_unit_price=84.22, parking_ping=0),
                deal(house_unit_price=85.26, parking_ping=0),
                deal(house_unit_price=85.51, parking_ping=0),
                deal(house_unit_price=97.47, parking_ping=0),
            ]
        )
        self.assertEqual(n, 5)
        self.assertEqual(low, 83.21)
        self.assertEqual(high, 97.47)

    def test_split_address_keeps_main_door_not_subno(self):
        want = _split_tw_address("台北市內湖區康寧路三段54-5號")
        self.assertEqual(want["city"], "台北市")
        self.assertEqual(want["district"], "內湖區")
        self.assertEqual(want["road"], "康寧路三段")
        self.assertEqual(want["number"], "54")


class FloorParseTests(unittest.TestCase):
    def test_listing_floor(self):
        self.assertEqual(parse_listing_floor("3F/5F"), 3)
        self.assertEqual(parse_listing_floor("5F/9F"), 5)
        self.assertEqual(parse_listing_floor("B1/5F"), -1)
        self.assertIsNone(parse_listing_floor("頂樓加蓋"))
        self.assertIsNone(parse_listing_floor("整棟"))
        self.assertIsNone(parse_listing_floor("4-5樓/5樓"))
        self.assertIsNone(parse_listing_floor("4~5/5樓"))

    def test_plvr_floor(self):
        self.assertEqual(plvr._floor_to_int("五層"), 5)
        self.assertEqual(plvr._floor_to_int("十二層"), 12)
        self.assertEqual(plvr._floor_to_int("五層之2"), 5)
        self.assertEqual(plvr._floor_to_int("地下一層"), -1)
        self.assertIsNone(plvr._floor_to_int("全層"))
        self.assertIsNone(plvr._floor_to_int("一層，二層，三層"))
        self.assertEqual(plvr._floor_to_int("七層，電梯樓梯間"), 7)


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

    def test_unknown_parking_ping_does_not_force_zero(self):
        with_park = deal(total_ping=66.86, parking_ping=8.0, house_ping=58.86, floor_no=7, floor="七層")
        found, _ = plvr.find_previous_sale(
            [with_park], 66.86, parking_ping=None, floor_no=7, parking_known=False
        )
        self.assertIs(found, with_park)


class ParkingParseTests(unittest.TestCase):
    def test_bundled_parking_when_area_is_dash(self):
        ping, status, bundled = _parse_parking_info(
            {"車位面積": "-", "登記總面積": "66.86坪"},
            {"parking": "平面式，已含售金內", "unitArea": "66.86坪"},
        )
        self.assertIsNone(ping)
        self.assertTrue(bundled)
        self.assertIn("有", status)
        self.assertIn("平面式", status)
        self.assertIn("坪數未獨立標示", status)

    def test_area_text_says_includes_parking(self):
        ping, status, bundled = _parse_parking_info(
            {"車位面積": "-", "登記總面積": "66.86坪含車位"},
            {"parking": "", "unitArea": "66.86坪含車位"},
        )
        self.assertIsNone(ping)
        self.assertTrue(bundled)
        self.assertTrue(status.startswith("有"))

    def test_separate_parking_ping(self):
        ping, status, bundled = _parse_parking_info(
            {"車位面積": "5.64坪"},
            {"parking": "1個平面式車位，已含在售價內"},
        )
        self.assertAlmostEqual(ping, 5.64)
        self.assertFalse(bundled)
        self.assertEqual(status, "有（平面式）")

    def test_no_parking(self):
        ping, status, bundled = _parse_parking_info(
            {"車位面積": "-"},
            {"parking": "無車位"},
        )
        self.assertIsNone(ping)
        self.assertEqual(status, "無")
        self.assertFalse(bundled)

    def test_public_ratio_note_and_registered_area(self):
        self.assertEqual(_annotate_public_ratio("40%", True), "40%（需另外扣除車位）")
        self.assertEqual(
            _annotate_public_ratio("35%（本戶刊登 40%）", True),
            "35%（本戶刊登 40%；需另外扣除車位）",
        )
        self.assertEqual(_annotate_public_ratio("40%", False), "40%")
        self.assertEqual(
            _format_registered_area("66.86坪", None, has_parking=True, bundled=True),
            "66.86坪（含車位，坪數未獨立標示）",
        )
        self.assertEqual(_format_registered_area("20.69坪", 5.64), "20.69坪（內含車位 5.64坪）")
        self.assertEqual(_format_registered_area("20坪", None), "20.00坪（無車位）")


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
        self.assertEqual(_parse_591_id("https://www.591.com.tw/home/house/detail/2/20676115.html"), "20676115")
        self.assertEqual(_parse_591_id("20676115"), "20676115")
        self.assertEqual(
            _parse_591_id("https://sale.591.com.tw/home/housing/7479?id=20676115"),
            "20676115",
        )

    def test_591_community_overview_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_591_id("https://sale.591.com.tw/home/housing/7479")
        self.assertIn("社區總覽", str(ctx.exception))

    def test_portal_community_and_listing_urls(self):
        self.assertEqual(
            _parse_sinyi_id("https://www.sinyi.com.tw/buy/house/9583ZT?from=community"),
            "9583ZT",
        )
        self.assertEqual(_parse_sinyi_id("https://www.sinyi.com.tw/buy?hid=9583ZT"), "9583ZT")
        self.assertEqual(
            _parse_yungching_id("https://buy.yungching.com.tw/house/7106341?from=community"),
            "7106341",
        )
        self.assertEqual(
            _parse_housefun_id("https://buy.housefun.com.tw/buy/house/6802942?community=1"),
            "6802942",
        )
        self.assertEqual(_parse_etwarm_id("https://www.etwarm.com.tw/houses/buy/707984"), "707984")
        self.assertEqual(_parse_twhg_id("https://www.twhg.com.tw/buy/TA02534212"), "TA02534212")
        self.assertEqual(_parse_twhg_id("https://www.twhg.com.tw/object/TA02534212"), "TA02534212")
        self.assertEqual(_parse_hbhousing_id("https://www.hbhousing.com.tw/detail?sn=YS203907"), "YS203907")
        self.assertEqual(_parse_hbhousing_id("https://www.hbhousing.com.tw/community/x?sn=YS203907"), "YS203907")
        self.assertEqual(
            _rakuya_ehid("https://community.rakuya.com.tw/38039/sell/info?ehid=0b0136347737533&from=communitySearchSell"),
            "0b0136347737533",
        )
        self.assertEqual(
            _rakuya_ehid("https://www.rakuya.com.tw/sell_item/info?ehid=0b0136347737533"),
            "0b0136347737533",
        )
        self.assertEqual(_rakuya_ehid("https://community.rakuya.com.tw/38039/sell"), "")

    def test_rakuya_translate_proxy_url(self):
        url = _google_translate_proxy_url(
            "https://www.rakuya.com.tw/sell_item/info?ehid=0b0136347737533"
        )
        self.assertIn("www-rakuya-com-tw.translate.goog", url)
        self.assertIn("ehid=0b0136347737533", url)
        self.assertIn("_x_tr_pto=wapp", url)

    def test_rakuya_community_page_counts_as_ready(self):
        self.assertTrue(_rakuya_page_ready('window.apiCommunityInfoData = {"data":{}}'))
        self.assertTrue(_rakuya_page_ready("window.itemInfo = {}"))
        self.assertFalse(_rakuya_page_ready("<html>Just a moment</html>"))
        listing = PortalListing(source_name="樂屋網", listing_id="x", source_url="https://www.rakuya.com.tw/")
        self.assertEqual(
            _rakuya_community_url(
                "https://community.rakuya.com.tw/38039/sell/info?ehid=0b0136347737533",
                listing,
                {},
            ),
            "https://community.rakuya.com.tw/38039",
        )


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


class PortalParseTests(unittest.TestCase):
    def test_price_to_wan(self):
        self.assertEqual(_price_to_wan(25880000), 2588)
        self.assertEqual(_price_to_wan("2,588"), 2588)
        self.assertEqual(_price_to_wan(2600), 2600)

    def test_sinyi_jsonld_and_labels(self):
        html = """
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"RealEstateListing","name":"永春捷運獨棟空間大","about":{"@type":"Apartment","address":{"addressRegion":"台北市","addressLocality":"信義區","streetAddress":"永吉路"},"floorSize":{"value":43.22,"unitText":"坪"},"additionalProperty":[{"name":"格局","value":"5房4廳2.5衛"}]},"offers":{"price":25880000,"priceCurrency":"TWD"}}</script>
        <div>屋齡</div><div>36.7年</div>
        <div>樓層</div><div>4-5樓/5樓</div>
        <div>車位</div><div>--</div>
        <img src="https://res.sinyi.com.tw/buy/0051HR/bigimg/A.JPG">
        """
        listing = parse_sinyi_html(html, "https://www.sinyi.com.tw/buy/house/0051HR")
        self.assertEqual(listing.listing_id, "0051HR")
        self.assertEqual(listing.title, "永春捷運獨棟空間大")
        self.assertEqual(listing.ask_price_wan, 2588)
        self.assertEqual(listing.listing_address, "台北市信義區永吉路")
        self.assertIn("43.22", listing.registered)
        self.assertEqual(listing.house_age, "36.7年")
        self.assertEqual(listing.floor, "4-5樓/5樓")
        self.assertEqual(listing.parking_desc, "無車位")
        self.assertFalse(listing.parking_unknown)
        self.assertTrue(listing.images)

    def test_yungching_pairs_and_age(self):
        html = """
        <script type="application/ld+json">{"@type":"Product","name":"永春捷運一層一戶 | 台北市信義區永吉路","offers":{"price":2600,"priceCurrency":"TWD"},"image":"https://yccdn.yungching.com.tw/v1/image/?key=abc"}</script>
        <script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[{"name":"買屋"},{"name":"台北市"},{"name":"信義區"},{"name":"永吉路"}]}</script>
        <div class="item-title"><h4>建物坪數</h4></div><div class="item-detail"><span>43.22坪</span></div>
        <div class="item-title">・主建物</div><div class="item-detail">25.47坪</div>
        <div class="item-title"><h4>謄本用途</h4></div><div class="item-detail">住家用</div>
        <div>屋齡36.6 年</div>
        <div>4~5/5樓</div>
        """
        listing = parse_yungching_html(html, "https://buy.yungching.com.tw/house/7106341")
        self.assertEqual(listing.listing_id, "7106341")
        self.assertEqual(listing.title, "永春捷運一層一戶")
        self.assertEqual(listing.ask_price_wan, 2600)
        self.assertEqual(listing.registered, "43.22坪")
        self.assertEqual(listing.main_area, "25.47坪")
        self.assertEqual(listing.house_age, "36.6年")
        self.assertIn("4~5/5樓", listing.floor)
        self.assertTrue(listing.parking_unknown)
        self.assertEqual(listing.registered_use, "住家用")

    def test_rakuya_item_info(self):
        html = """
        <script>
        window.itemInfo = {"title":{"hname":"仁二路面寬透店","address":"基隆市仁愛區仁二路","community":""},"price":{"price":"10,500","singlePrice":"70","totalsize":"150"},"detail":{"ageValue":61.6,"ageUnit":"年","transFloors":"1","maxFloors":"3","patternBedrooms":2,"patternLivingrooms":2,"patternBathrooms":1,"itemUseType":"住辦/透天厝","mainSize":"150坪","parking":"無車位","parkingSize":"","ehid":"07547d349748407"},"images":{"photo":[{"url":"https://static.rakuya.com.tw/a.jpg"}]},"special":{"description":"仁二路面寬透店"}};
        </script>
        """
        listing = parse_rakuya_html(html, "https://www.rakuya.com.tw/sell_item/info?ehid=07547d349748407")
        self.assertEqual(listing.listing_id, "07547d349748407")
        self.assertEqual(listing.title, "仁二路面寬透店")
        self.assertEqual(listing.ask_price_wan, 10500)
        self.assertEqual(listing.ask_unit_price, "70萬/坪")
        self.assertEqual(listing.registered, "150坪")
        self.assertEqual(listing.house_age, "61.6年")
        self.assertEqual(listing.floor, "1F/3F")
        self.assertEqual(listing.parking_desc, "無車位")
        self.assertEqual(listing.layout, "2房2廳1衛")

    def test_rakuya_community_url_and_sur_floors(self):
        html = """
        <script>
        window.itemInfo = {"title":{"hname":"葫洲觀湖兩房","address":"台北市內湖區康寧路三段","community":"觀湖"},"price":{"price":"2,188","singlePrice":"114.14","totalsize":"19.17"},"detail":{"ageValue":"約30","ageUnit":"年","transFloors":"6","maxFloors":"","surFloors":"10","patternBedrooms":2,"patternLivingrooms":2,"patternBathrooms":1,"itemUseType":"電梯大廈","propertyUsecode":"住家用","mainSize":"11.66坪","shareSize":"2.52坪","parking":"-","parkingGarage":"無","parkingSize":"","ehid":"0b0136347737533"},"nearby":{"nearByCommunity":"觀湖","groupId":"sinyi/9583ZT"},"images":{"photo":[]},"special":{"description":""}};
        </script>
        """
        listing = parse_rakuya_html(
            html,
            "https://community.rakuya.com.tw/38039/sell/info?ehid=0b0136347737533&from=communitySearchSell",
        )
        self.assertEqual(listing.listing_id, "0b0136347737533")
        self.assertEqual(listing.community_name, "觀湖")
        self.assertEqual(listing.floor, "6F/10F")
        self.assertEqual(listing.parking_desc, "無車位")
        self.assertFalse(listing.parking_unknown)
        self.assertEqual(listing.registered_use, "住家用")
        self.assertEqual(listing.ask_price_wan, 2188)
        self.assertEqual(_rakuya_ehid("https://community.rakuya.com.tw/38039/sell/info?ehid=0b0136347737533&from=communitySearchSell"), "0b0136347737533")

    def test_rakuya_community_page_fills_missing_env(self):
        listing = parse_rakuya_html(
            """
            <script>
            window.itemInfo = {"title":{"hname":"葫洲觀湖兩房","address":"台北市內湖區康寧路三段","community":"觀湖","communityUrl":"https://community.rakuya.com.tw/38039"},"price":{"price":"2,188","totalsize":"19.17"},"detail":{"transFloors":"6","surFloors":"10","ehid":"0b0136347737533","parking":"-","parkingGarage":"無"},"nearby":{},"images":{"photo":[]},"special":{}};
            </script>
            """,
            "https://www.rakuya.com.tw/sell_item/info?ehid=0b0136347737533",
        )
        html = """
        <script>
        window.apiCommunityInfoData = {"status":true,"data":{"name":"民權觀湖","households":"175戶","maxFloor":"10、12~13樓","shareRatio":"14.17%","address":{"cityName":"台北市","areaName":"內湖區","full":"康寧路三段54-5號"},"location":{"lat":25.0714,"lon":121.6094},"building":{"baseArea":"1,210坪"}}};
        window.apiNearPoi = {"status":true,"data":[{"type":"transport","poiList":[{"cat":"出入口","name":"捷運葫洲站出口1","distance":173}]},{"type":"market","poiList":[{"cat":"超級市場","name":"家樂福內湖民權東店","distance":199}]}]};
        </script>
        """
        apply_rakuya_community(listing, html)
        self.assertEqual(listing.community_name, "民權觀湖")
        self.assertEqual(listing.household_count, "175戶")
        self.assertEqual(listing.land_area, "1,210坪")
        self.assertEqual(listing.building_floors, "10、12~13樓")
        self.assertIn("葫洲", listing.nearest_mrt)
        self.assertIn("家樂福", listing.nearest_shop)
        self.assertAlmostEqual(listing.lat, 25.0714)
        self.assertIn("54-5號", listing.listing_address)

    def test_hbhousing_nuxt_payload(self):
        html = """
        <script id="__NUXT_DATA__" type="application/json">[null,null,{"sn":3,"price":4,"area":5,"objName":6,"age":7,"floor":8,"floorTotal":9,"type":10,"parking":11,"city":12,"district":13,"road":14,"mainArea":15,"uprice":16,"lat":17,"lon":18}, "YS203907", 2798, 70.13, "北投復興崗收租店面", 35.2, "1", "5", "商業用", "無", "台北市", "北投區", "中央北路二段", 27.91, 39.9, 25.13, 121.49]</script>
        """
        listing = parse_hbhousing_html(html, "https://www.hbhousing.com.tw/detail?sn=YS203907")
        self.assertEqual(listing.listing_id, "YS203907")
        self.assertEqual(listing.title, "北投復興崗收租店面")
        self.assertEqual(listing.ask_price_wan, 2798)
        self.assertEqual(listing.registered, "70.13坪")
        self.assertEqual(listing.house_age, "35.2年")
        self.assertEqual(listing.floor, "1F/5F")
        self.assertEqual(listing.parking_desc, "無車位")
        self.assertIn("北投區", listing.listing_address)

    def test_twhg_labels(self):
        html = """
        <h1>綠意時尚汐止低總三房華廈 (TA02534212)</h1>
        <div>屋齡</div><div>32年2月</div>
        <div>樓層</div><div>4/8樓</div>
        <div>建坪</div><div>29.49 坪</div>
        <div>格局</div><div>3房2廳2衛</div>
        <div>地址</div><div>新北市汐止區汐平路一段</div>
        <div>車位</div><div>無車位</div>
        <div>1,298 萬</div>
        """
        listing = parse_twhg_html(html, "https://www.twhg.com.tw/buy/TA02534212")
        self.assertEqual(listing.listing_id, "TA02534212")
        self.assertEqual(listing.ask_price_wan, 1298)
        self.assertEqual(listing.registered, "29.49 坪")
        self.assertEqual(listing.house_age, "32年2月")
        self.assertEqual(listing.floor, "4/8樓")
        self.assertEqual(listing.parking_desc, "無車位")

    def test_housefun_jsonld(self):
        html = """
        <script type="application/ld+json">{"@graph":[{"@type":"Residence","address":{"streetAddress":"台北市萬華區漢中街"},"additionalProperty":[{"name":"floorSize","value":{"value":"33.67 坪"}},{"name":"numberOfRooms","value":{"value":"2"}},{"name":"numberOfLivingRoomTotal","value":{"value":"2"}},{"name":"numberOfBathRoomsTotal","value":{"value":"2"}},{"name":"yearBuilt","value":{"value":"4"}}]},{"@type":"Product","name":"A15西門全新2房2衛","description":"8/15樓","offers":{"price":"37700000","priceCurrency":"TWD"}}]}</script>
        """
        listing = parse_housefun_html(html, "https://buy.housefun.com.tw/buy/house/6802942")
        self.assertEqual(listing.listing_id, "6802942")
        self.assertEqual(listing.ask_price_wan, 3770)
        self.assertIn("33.67", listing.registered)
        self.assertEqual(listing.house_age, "4年")
        self.assertEqual(listing.listing_address, "台北市萬華區漢中街")

    def test_etwarm_labels(self):
        html = """
        <title>東森-凡爾賽三房平車美廈 - 東森房屋</title>
        <div>總價</div><div>858 萬</div>
        <div>地址</div><div>台中市豐原區中山路</div>
        <div>格局</div><div>3房/ 1廳/ 2衛</div>
        <div>樓層</div><div>2F / 5F</div>
        <div>屋齡</div><div>34.6年</div>
        <div>車位</div><div>－</div>
        <div>建物總坪數</div><div>約43.4坪</div>
        """
        listing = parse_etwarm_html(html, "https://www.etwarm.com.tw/houses/buy/707984")
        self.assertEqual(listing.listing_id, "707984")
        self.assertEqual(listing.ask_price_wan, 858)
        self.assertEqual(listing.listing_address, "台中市豐原區中山路")
        self.assertEqual(listing.house_age, "34.6年")
        self.assertEqual(listing.parking_desc, "無車位")
        self.assertIn("43.4", listing.registered)


class CthouseParseTests(unittest.TestCase):
    def test_cthouse_id_from_url(self):
        self.assertEqual(_parse_cthouse_id("https://buy.cthouse.com.tw/house/1083GN-sell.html"), "1083GN")
        self.assertEqual(_parse_cthouse_id("https://buy.cthouse.com.tw/house/2097113.html"), "2097113")

    def test_cthouse_listing_fields(self):
        data = {
            "case_name": "大毅好幸福三房平車露台戶",
            "sell_price": "1740",
            "unit_price": 32.57,
            "house_area": 53.43,
            "main_build": 25.75,
            "age_val": "9年",
            "floor_val": "3樓/共15樓",
            "r_type": "3房2廳2衛",
            "address": "臺中市太平區立文街",
            "postulate_ratio": 32.492,
            "month_fee": 4.62,
            "parking_lot_belong": 1,
            "parking_lot_price": 0.0,
            "latitude": 24.159,
            "longitude": 120.716,
            "direction": "座北朝南",
            "object_tag": [["1", "大毅好幸福", "/community/20190716215002380.html"]],
            "s_imgs": ["https://media.cthouse.com.tw/photo/project2/house_photo/202603/2097113-1s_new.jpg"],
        }
        listing = parse_cthouse_listing(
            data,
            "https://buy.cthouse.com.tw/house/1083GN-sell.html",
            "1083GN",
        )
        self.assertEqual(listing.listing_id, "1083GN")
        self.assertEqual(listing.ask_price_wan, 1740)
        self.assertEqual(listing.community_name, "大毅好幸福")
        self.assertEqual(listing.floor, "3樓/共15樓")
        self.assertIn("含在售價內", listing.parking_desc)
        self.assertTrue(listing.images[0].endswith("_new.jpg"))


class CommunityMatchTests(unittest.TestCase):
    def test_ignore_related_community_links_when_unbound(self):
        html = """
        <input id="hid_communityId" value="0">
        <a href="https://market.591.com.tw/7479">中山文華-文華滙</a>
        """
        self.assertEqual(_community_id_from_html(html), "")

    def test_bound_community_id(self):
        html = '<input id="hid_communityId" value="12345">'
        self.assertEqual(_community_id_from_html(html), "12345")

    def test_name_from_meta_located_in(self):
        html = '<meta name="description" content="台北市內湖區住宅出售：總價4388萬，面積49.18坪，位於皇翔維也納皇后區，更多出售詳情">'
        name = _community_name_from_listing(html, {"title": "【寬悅團隊】☆維也納皇后區☆三房", "communityName": ""})
        self.assertEqual(name, "皇翔維也納皇后區")

    def test_reject_other_district_community(self):
        base = {"address": {"region": "台北市", "section": "內湖區", "lat": "25.076", "lng": "121.576"}}
        community = {"name": "中山文華-文華滙", "address": "台北市大同區承德路二段69號"}
        detail = {"section": "大同區", "lat": "25.055", "lng": "121.518"}
        self.assertFalse(_community_belongs_to_listing(community, detail, base))

    def test_accept_same_district_community(self):
        base = {"address": {"region": "台北市", "section": "內湖區", "lat": "25.076", "lng": "121.576"}}
        community = {"name": "皇翔維也納皇后區", "address": "台北市內湖區江南街"}
        detail = {"section": "內湖區", "lat": "25.076", "lng": "121.576"}
        self.assertTrue(_community_belongs_to_listing(community, detail, base))

    def test_leju_does_not_pick_unrelated_same_district_name(self):
        candidates = [
            {"id": "L1", "name": "承德大樓(承德路二段)", "city": "台北市", "district": "大同區"},
            {"id": "L2", "name": "中山文華文華滙", "city": "台北市", "district": "大同區"},
        ]
        want = {"city": "台北市", "district": "大同區", "road": "承德路二段"}
        picked = _pick_leju_candidate(candidates, want, "皇翔維也納皇后區")
        self.assertIsNone(picked)
        picked = _pick_leju_candidate(candidates, want, "中山文華-文華滙")
        self.assertEqual(picked["id"], "L2")

    def test_leju_does_not_treat_similar_community_as_same(self):
        self.assertFalse(_leju_names_compatible("觀湖", "民權觀湖"))
        self.assertEqual(_leju_name_score("觀湖", "民權觀湖"), 1)
        self.assertTrue(_leju_names_compatible("觀湖", "觀湖"))
        self.assertTrue(_leju_names_compatible("觀湖", "觀湖一期"))
        candidates = [
            {"id": "L1", "name": "民權觀湖", "city": "台北市", "district": "內湖區"},
            {"id": "L2", "name": "觀湖", "city": "台北市", "district": "內湖區"},
        ]
        want = {"city": "台北市", "district": "內湖區", "road": "康寧路三段"}
        picked = _pick_leju_candidate(candidates, want, "觀湖")
        self.assertEqual(picked["id"], "L2")
        picked = _pick_leju_candidate(
            [{"id": "L1", "name": "民權觀湖", "city": "台北市", "district": "內湖區"}],
            want,
            "觀湖",
        )
        self.assertEqual(picked["id"], "L1")
        self.assertTrue(
            _leju_result_matches(
                {"name": "民權觀湖", "address": "康寧路三段54-5號"},
                {"id": "L1", "name": "民權觀湖"},
                want,
                "觀湖",
            )
        )
        self.assertFalse(
            _leju_result_matches(
                {"name": "民權觀湖", "address": "民權東路六段"},
                {"id": "L1", "name": "民權觀湖"},
                want,
                "觀湖",
            )
        )


if __name__ == "__main__":
    unittest.main()
