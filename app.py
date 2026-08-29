import html
import os
import re
import threading
import urllib.parse

import streamlit as st
import streamlit.components.v1 as components

import plvr
from scraper import AnalysisReport, analyze_url

try:
    google_key = st.secrets.get("GOOGLE_MAPS_API_KEY", "")
    if google_key:
        os.environ["GOOGLE_MAPS_API_KEY"] = google_key
except Exception:
    pass

st.set_page_config(
    page_title="房產分析助手",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
<style>
    @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700&display=swap');

    html, body, [class*="css"] {
        font-family: 'Noto Sans TC', sans-serif;
    }

    .block-container {
        padding-top: 0.5rem;
        max-width: 1200px;
    }

    div[data-testid="stVerticalBlock"]:has(.sticky-header) {
        position: sticky;
        top: 0;
        z-index: 999;
        background: rgba(255, 255, 255, 0.97);
        backdrop-filter: blur(10px);
        padding: 0.75rem 0 0.9rem 0;
        margin: 0 -1rem;
        padding-left: 1rem;
        padding-right: 1rem;
        border-bottom: 1px solid #e2e8f0;
        box-shadow: 0 6px 24px rgba(15, 23, 42, 0.07);
    }

    .sticky-title {
        font-size: 1.05rem;
        font-weight: 700;
        color: #0f172a;
        margin: 0 0 0.55rem 0;
    }

    .section-card {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 14px;
        padding: 1.1rem 1.3rem;
        margin-bottom: 1rem;
    }

    .section-title {
        font-size: 1.15rem;
        font-weight: 700;
        color: #0f172a;
        margin-bottom: 0.8rem;
        padding-bottom: 0.5rem;
        border-bottom: 2px solid #14b8a6;
        display: inline-block;
    }

    .field-label {
        font-size: 0.78rem;
        font-weight: 600;
        color: #64748b;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        margin-bottom: 0.15rem;
    }

    .field-value {
        font-size: 0.98rem;
        color: #0f172a;
        margin-bottom: 0.75rem;
        line-height: 1.5;
    }

    .pro-item {
        background: #ecfdf5;
        border-left: 4px solid #10b981;
        padding: 0.65rem 0.85rem;
        border-radius: 8px;
        margin-bottom: 0.5rem;
        color: #065f46;
        font-size: 0.92rem;
    }

    .con-item {
        background: #fff7ed;
        border-left: 4px solid #f59e0b;
        padding: 0.65rem 0.85rem;
        border-radius: 8px;
        margin-bottom: 0.5rem;
        color: #92400e;
        font-size: 0.92rem;
    }

    .photo-caption {
        font-size: 0.82rem;
        color: #64748b;
        margin-top: -0.35rem;
        margin-bottom: 0.75rem;
    }

    div[data-testid="stRadio"] > div {
        background: #f1f5f9;
        padding: 0.35rem;
        border-radius: 10px;
        gap: 0.25rem;
    }

    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #0d9488, #0891b2);
        border: none;
        border-radius: 10px;
        font-weight: 600;
    }

    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
</style>
""",
    unsafe_allow_html=True,
)

if "single_url" not in st.session_state:
    st.session_state.single_url = ""
if "compare_urls" not in st.session_state:
    st.session_state.compare_urls = ""
if "single_report" not in st.session_state:
    st.session_state.single_report = None
if "compare_reports" not in st.session_state:
    st.session_state.compare_reports = None


@st.cache_resource(show_spinner=False)
def _start_plvr_warmup() -> bool:
    """伺服器啟動後在背景預載六都實價登錄，縮短首次分析等待時間。"""

    def _run() -> None:
        try:
            plvr.warm_common_cities(season_count=2)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return True


_start_plvr_warmup()


@st.cache_data(ttl=1800, show_spinner=False)
def cached_analyze_url(url: str) -> AnalysisReport:
    return analyze_url(url)


def clear_single_url() -> None:
    st.session_state.single_url = ""


def clear_compare_urls() -> None:
    st.session_state.compare_urls = ""


def show_field(label: str, value: str) -> None:
    safe_value = html.escape(value or "查無資料")
    st.markdown(f'<div class="field-label">{label}</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="field-value">{safe_value}</div>', unsafe_allow_html=True)


def section_header(title: str) -> None:
    st.markdown(f'<div class="section-title">{title}</div>', unsafe_allow_html=True)


def _render_price_gap(report: AnalysisReport) -> None:
    """把開價放到合理區間與前次成交價旁邊，讓議價空間一眼看得出來。"""
    ask = report.ask_price_wan
    if not ask:
        return

    cols = st.columns(3)
    cols[0].metric("開價", f"{ask:,.0f} 萬")

    upper = re.search(r"～\s*([\d,]+)\s*萬", report.total_price_range or "")
    if upper:
        top = float(upper.group(1).replace(",", ""))
        cols[1].metric("較合理區間上緣", f"{ask - top:+,.0f} 萬", delta=f"{(ask / top - 1) * 100:+.1f}%")

    previous = re.search(r"以\s*([\d,]+)\s*萬成交", getattr(report, "previous_sale", "") or "")
    if previous:
        before = float(previous.group(1).replace(",", ""))
        cols[2].metric("較前次成交", f"{ask - before:+,.0f} 萬", delta=f"{(ask / before - 1) * 100:+.1f}%")


def _render_building_deals(report: AnalysisReport) -> None:
    """列出同棟的實價登錄明細，讓使用者能自己核對估價依據。"""
    deals = getattr(report, "building_deals", None)
    if not deals:
        return

    scope = getattr(report, "building_deal_scope", "") or "同棟"
    with st.expander(f"同棟實價登錄明細（{len(deals)} 筆，比對方式：{scope}）"):
        table = {
            "成交年月": [d.date for d in deals],
            "門牌": [d.address for d in deals],
            "樓層": [d.floor or "—" for d in deals],
            "格局": [d.layout or "—" for d in deals],
            "權狀坪": [f"{d.total_ping:.2f}" for d in deals],
            "車位坪": [f"{d.parking_ping:.2f}" if d.parking_ping else "—" for d in deals],
            "總價(萬)": [f"{d.total_wan:,.0f}" for d in deals],
            "車位價(萬)": [f"{d.parking_wan:,.0f}" if d.parking_wan else "—" for d in deals],
            "房屋單價": [f"{d.house_unit_price:.1f}" if d.house_unit_price else "無法拆分" for d in deals],
            "備註": [d.note.rstrip("；") or "—" for d in deals],
        }
        st.dataframe(table, use_container_width=True, hide_index=True)
        st.caption("房屋單價為扣除車位後的萬/坪；車位未單獨申報價格時無法拆分。資料來源：內政部實價登錄。")


def _render_591_deals(report: AnalysisReport) -> None:
    deals = getattr(report, "deals", None)
    if not deals:
        return
    with st.expander(f"591 社區近期成交（{len(deals)} 筆，可能比官方開放資料新）"):
        table = {
            "成交年月": [d.date for d in deals],
            "樓層": [d.floor or "—" for d in deals],
            "格局": [d.layout or "—" for d in deals],
            "坪數": [f"{d.area_ping:.2f}" if d.area_ping else "—" for d in deals],
            "單價(萬/坪)": [f"{d.unit_price:.1f}" if d.unit_price else "—" for d in deals],
            "總價(萬)": [f"{d.total_wan:,.0f}" if d.total_wan else "—" for d in deals],
            "車位": [d.parking or "—" for d in deals],
        }
        st.dataframe(table, use_container_width=True, hide_index=True)
        st.caption("591 的單價與坪數多未拆車位，且門牌常被遮蔽，僅供對照官方資料的時間落差，不作為本戶前次成交依據。")


def render_photo_grid(images: list[str], columns: int = 3, max_images: int = 9) -> None:
    if not images:
        st.caption("查無照片")
        return
    cols = st.columns(columns)
    for idx, url in enumerate(images[:max_images]):
        with cols[idx % columns]:
            st.image(url, use_container_width=True)


def render_google_map(report: AnalysisReport) -> None:
    map_address = report.community_address
    listing_address = getattr(report, "listing_address", "")
    display_addr = ""
    if map_address and map_address != "查無資料":
        display_addr = map_address.split("（")[0].strip()
    elif listing_address and listing_address != "查無資料":
        display_addr = listing_address.split("（")[0].strip()

    if report.latitude and report.longitude:
        query = f"{report.latitude},{report.longitude}"
    elif display_addr:
        query = display_addr
    else:
        st.caption("無法取得社區登記地址，無法顯示地圖。")
        return

    st.markdown(f"**{html.escape(display_addr or query)}**")

    map_url = (
        "https://maps.google.com/maps?"
        f"q={urllib.parse.quote(query)}&hl=zh-TW&z=16&output=embed"
    )
    components.html(
        f"""
        <iframe
            src="{html.escape(map_url)}"
            width="100%"
            height="420"
            style="border:0;border-radius:14px;"
            allowfullscreen=""
            loading="lazy"
            referrerpolicy="no-referrer-when-downgrade">
        </iframe>
        """,
        height=440,
    )
    maps_link = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(query)}"
    st.markdown(f"[在 Google 地圖中開啟]({maps_link})")


def render_report(report: AnalysisReport, show_market: bool = True, compact: bool = False) -> None:
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    section_header("1. 社區環境")
    c1, c2 = st.columns(2)
    with c1:
        show_field("(1) 社區名稱", report.community_name)
        show_field("(2) 基地面積", report.land_area)
        show_field("(3) 總戶數", report.household_count)
        show_field("(4) 總樓高", report.building_floors)
    with c2:
        show_field("(5) 公設比", report.public_ratio)
        show_field("(6) 社區地址", report.community_address)
        show_field("(7) 最近捷運站", report.nearest_mrt)
        show_field("(8) 最近超市", report.nearest_supermarket)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    section_header("2. 本戶條件")
    h1, h2 = st.columns(2)
    with h1:
        show_field("(1) 權狀面積", report.registered_area)
        show_field("(2) 有無車位", getattr(report, "parking_status", "無"))
        show_field("(3) 屋齡", report.house_age)
        show_field("(4) 主建物面積", report.main_building_area)
    with h2:
        show_field("(5) 登記用途", report.registered_use)
        show_field("(6) 樓層", report.floor)
        show_field("(7) 廁所是否有對外窗", report.bathroom_window)
        show_field("(8) 有幾面採光", report.lighting_faces)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    section_header("3. 優缺點分析")
    p1, p2 = st.columns(2)
    with p1:
        st.markdown("**優點**")
        for item in report.pros:
            st.markdown(f'<div class="pro-item">{html.escape(item)}</div>', unsafe_allow_html=True)
    with p2:
        st.markdown("**缺點**")
        for item in report.cons:
            st.markdown(f'<div class="con-item">{html.escape(item)}</div>', unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)

    if show_market:
        st.markdown('<div class="section-card">', unsafe_allow_html=True)
        section_header("4. 市場行情")
        show_field("合理單價區間（不含車位）", report.unit_price_range)
        show_field("合理總價區間（含車位）", report.total_price_range)
        show_field("本戶前次成交", getattr(report, "previous_sale", "查無資料"))
        _render_price_gap(report)
        st.info(report.market_comment)
        _render_building_deals(report)
        _render_591_deals(report)
        st.markdown("</div>", unsafe_allow_html=True)

    photo_cols = 2 if compact else 3
    photo_max = 4 if compact else 9

    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    section_header("5. 房屋內部照片")
    st.markdown('<div class="photo-caption">來自本戶刊登照片</div>', unsafe_allow_html=True)
    render_photo_grid(report.interior_images, columns=photo_cols, max_images=photo_max)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    section_header("6. 社區公共空間")
    st.markdown('<div class="photo-caption">外觀、環境、公設等社區照片</div>', unsafe_allow_html=True)
    render_photo_grid(report.community_images, columns=photo_cols, max_images=photo_max)
    st.markdown("</div>", unsafe_allow_html=True)

    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    section_header("7. 位置地圖")
    render_google_map(report)
    st.markdown("</div>", unsafe_allow_html=True)

    if not compact:
        with st.expander("物件編號與資料來源"):
            st.write(f"編號：{report.listing_id}")
            for source in report.sources:
                st.write(source)


def parse_urls(raw: str) -> list[str]:
    urls = []
    seen = set()
    for line in raw.splitlines():
        for part in line.replace("，", ",").split(","):
            url = part.strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def render_comparison_table(reports: list[AnalysisReport]) -> None:
    rows = [
        ("社區", [r.community_name for r in reports]),
        ("合理單價（不含車位）", [r.unit_price_range for r in reports]),
        ("合理總價（含車位）", [r.total_price_range for r in reports]),
        ("本戶前次成交", [getattr(r, "previous_sale", "查無資料") for r in reports]),
        ("權狀面積", [r.registered_area for r in reports]),
        ("有無車位", [getattr(r, "parking_status", "無") for r in reports]),
        ("主建物", [r.main_building_area for r in reports]),
        ("屋齡", [r.house_age for r in reports]),
        ("樓層", [r.floor for r in reports]),
        ("採光面數", [r.lighting_faces for r in reports]),
        ("廁所對外窗", [r.bathroom_window for r in reports]),
        ("最近捷運", [r.nearest_mrt for r in reports]),
        ("最近超市", [r.nearest_supermarket for r in reports]),
    ]
    table = {"項目": [row[0] for row in rows]}
    for idx in range(len(reports)):
        table[f"物件 {idx + 1}"] = [row[1][idx] for row in rows]
    st.dataframe(table, use_container_width=True, hide_index=True)


with st.container():
    st.markdown('<div class="sticky-header"></div>', unsafe_allow_html=True)
    st.markdown('<p class="sticky-title">🏠 房產分析助手</p>', unsafe_allow_html=True)
    mode = st.radio("分析模式", ["單一分析", "多物件比較"], horizontal=True, label_visibility="collapsed")

    if mode == "單一分析":
        c_input, c_clear, c_action = st.columns([7, 1.2, 1.5])
        with c_input:
            st.text_input(
                "房屋網址",
                placeholder="物件頁或社區裡的單一戶都可以：591、樂屋、信義、永慶、住商、台灣房屋、好房網、東森、中信…",
                key="single_url",
                label_visibility="collapsed",
            )
        with c_clear:
            st.button("🗑 清空", on_click=clear_single_url, use_container_width=True)
        with c_action:
            run_single = st.button("開始分析", type="primary", use_container_width=True)
    else:
        c_top1, c_top2 = st.columns([8, 1.5])
        with c_top1:
            st.caption("每行一個網址，或用逗號分隔（建議 2～4 間）")
        with c_top2:
            st.button("🗑 清空全部", on_click=clear_compare_urls, use_container_width=True)
        st.text_area(
            "多個房屋網址",
            placeholder="每行一個網址（物件頁或社區裡的單一戶都可以）",
            height=90,
            key="compare_urls",
            label_visibility="collapsed",
        )
        run_compare = st.button("開始比較", type="primary")

if mode == "單一分析" and run_single:
    url = st.session_state.single_url.strip()
    if not url:
        st.warning("請先貼上房屋網址。")
        st.session_state.single_report = None
    else:
        with st.status("正在分析…", expanded=True) as status:
            status.write("讀取物件資料…")
            try:
                st.session_state.single_report = cached_analyze_url(url)
                st.session_state.compare_reports = None
                status.update(label="分析完成", state="complete")
            except ValueError as exc:
                status.update(label="分析失敗", state="error")
                st.error(str(exc))
                st.session_state.single_report = None
            except Exception as exc:
                status.update(label="分析失敗", state="error")
                st.error(f"讀取失敗：{exc}")
                st.session_state.single_report = None

elif mode == "多物件比較" and run_compare:
    urls = parse_urls(st.session_state.compare_urls)
    if len(urls) < 2:
        st.warning("請至少貼上 2 個房屋網址。")
        st.session_state.compare_reports = None
    elif len(urls) > 4:
        st.warning("一次最多比較 4 間。")
        st.session_state.compare_reports = None
    else:
        reports: list[AnalysisReport] = []
        errors: list[str] = []
        progress = st.progress(0, text="準備中…")
        for idx, url in enumerate(urls):
            progress.progress((idx + 0.2) / len(urls), text=f"分析第 {idx + 1} / {len(urls)} 間…")
            try:
                reports.append(cached_analyze_url(url))
            except ValueError as exc:
                errors.append(f"第 {idx + 1} 間：{exc}")
            except Exception as exc:
                errors.append(f"第 {idx + 1} 間：{exc}")
        progress.progress(1.0, text="完成")

        for err in errors:
            st.error(err)

        if len(reports) >= 2:
            st.session_state.compare_reports = reports
            st.session_state.single_report = None
        elif len(reports) == 1:
            st.warning("只有 1 間成功，請檢查其他網址。")
            st.session_state.single_report = reports[0]
            st.session_state.compare_reports = None
        else:
            st.session_state.compare_reports = None

if mode == "單一分析" and st.session_state.single_report:
    render_report(st.session_state.single_report)
    st.caption("若網站閒置一段時間後首次開啟較慢，是雲端喚醒所致；實價登錄會在背景預載，第二次起會快很多。")

elif mode == "多物件比較" and st.session_state.compare_reports:
    reports = st.session_state.compare_reports
    st.markdown('<div class="section-card">', unsafe_allow_html=True)
    section_header("比較總表")
    render_comparison_table(reports)
    st.markdown("</div>", unsafe_allow_html=True)

    cols = st.columns(len(reports))
    for col, report in zip(cols, reports):
        with col:
            st.markdown(f"**{report.community_name}**")
            render_report(report, show_market=False, compact=True)
