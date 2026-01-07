# scripts/update_data.py
# -*- coding: utf-8 -*-

import json
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple, Optional
import requests
from bs4 import BeautifulSoup

import fitz  # PyMuPDF
from urllib.parse import urljoin


HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

TZ_CN = timezone(timedelta(hours=8))

# ===== 可抓取信息源（公开、结构相对稳定） =====
WGLJ_SCHEDULE_INDEX = "https://wglj.gz.gov.cn/ztmb/gzhyn/hdpq/mindex.html"  # 活动排期索引:contentReference[oaicite:4]{index=4}

DOUBAN_BASE = "https://guangzhou.douban.com/events/"
DOUBAN_PAGES = [
    ("douban/week-all",        DOUBAN_BASE + "week-all"),         # 最近一周·全部:contentReference[oaicite:5]{index=5}
    ("douban/week-music",      DOUBAN_BASE + "week-music"),       # 最近一周·音乐:contentReference[oaicite:6]{index=6}
    ("douban/week-drama",      DOUBAN_BASE + "week-drama"),       # 最近一周·戏剧:contentReference[oaicite:7]{index=7}
    ("douban/week-exhibition", DOUBAN_BASE + "week-exhibition"),  # 最近一周·展览:contentReference[oaicite:8]{index=8}
    ("douban/week-course",     DOUBAN_BASE + "week-course"),      # 最近一周·课程:contentReference[oaicite:9]{index=9}
]

GDMUSEUM_HOME = "s://www.gdmuseum.com/"          # 广东省博物馆首页（含“最新活动”）:contentReference[oaicite:10]{index=10}
GDMUSEUM_ACTIVITY_LIST = "s://www.gdmuseum.com/col108/list"  # “活动”列表页:contentReference[oaicite:11]{index=11}

GZMUSEUM_EXHIBITION_LIST = "s://www.guangzhoumuseum.cn/website_cn/Web/Exhibition/Exhibition.aspx"   # 广州博物馆专题展览列表:contentReference[oaicite:12]{index=12}
GZMUSEUM_EXHIBITION_PRE = "s://www.guangzhoumuseum.cn/website_cn/Web/Dynamic/Exhibition.aspx"      # 广州博物馆展览预告:contentReference[oaicite:13]{index=13}


# ===== 过滤规则（你不爱“探店打卡”）=====
BAD_KEYWORDS = [
    "探店", "网红", "出片", "约拍", "拍照打卡", "打卡地", "打卡点",
    "团购", "双人特惠", "必打卡", "种草", "咖啡探店"
]
# 这些词即使出现，也不一定要杀（比如“脱口秀约会”这种）
SOFT_BAD_KEYWORDS = ["约会", "情侣"]

def now_cn_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")

def http_get(url: str, timeout: int = 25) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

def http_get_bytes(url: str, timeout: int = 30) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content

def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 10) -> str:
    """
    从 PDF 提取文本。max_pages 用于限制速度（文旅局清单一般前几页就够）。
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    texts = []
    pages = min(len(doc), max_pages)
    for i in range(pages):
        page = doc.load_page(i)
        t = page.get_text("text") or ""
        texts.append(t)
    doc.close()
    return "\n".join(texts)

def find_pdf_links_in_page(page_url: str) -> List[str]:
    """
    打开文旅局页面，找出里面的 PDF 附件链接（尽量宽松匹配）。
    """
    html = http_get(page_url)
    soup = BeautifulSoup(html, "html.parser")
    pdfs = []
    for a in soup.select("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        # 直接指向 pdf
        if href.lower().endswith(".pdf") or ".pdf?" in href.lower():
            pdfs.append(urljoin(page_url, href))
            continue
        # 有些站点附件是 /download/xxx 但真实是 pdf：这里先收集可疑链接，后面用 Content-Type 判断
        if any(x in href.lower() for x in ["download", "attach", "附件", "file"]):
            pdfs.append(urljoin(page_url, href))
    # 去重
    seen = set()
    out = []
    for u in pdfs:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def split_events_from_pdf_text(pdf_text: str, source_pdf: str) -> List[Dict[str, Any]]:
    """
    把 PDF 文本粗解析为活动条目。
    策略：
    - 按行清洗
    - 识别含日期/时间的行作为起点
    - 生成 event：name/area/timeHint/notes/link
    解析不完美也没关系：至少让你在网页里直接看到“具体内容”，不必下载。
    """
    lines = [norm(x) for x in pdf_text.splitlines()]
    lines = [x for x in lines if x and len(x) >= 4]

    # 常见日期模式：1月7日、01月07日、2026年1月7日、1/7、01-07 等
    date_pat = re.compile(r"(\d{4}年)?\s*\d{1,2}\s*[月/.-]\s*\d{1,2}\s*(日)?")
    time_pat = re.compile(r"(\d{1,2}:\d{2})\s*[-~—–]\s*(\d{1,2}:\d{2})")

    events = []
    buf = []

    def flush_buf(buf_lines: List[str]):
        if not buf_lines:
            return
        block = " ".join(buf_lines)
        # 太短的不当活动
        if len(block) < 12:
            return

        # 提取时间提示
        m_time = time_pat.search(block)
        time_hint = m_time.group(0) if m_time else ""

        # 提取日期（若有）
        m_date = date_pat.search(block)
        date_hint = m_date.group(0) if m_date else ""

        # 粗提取地点（常见关键词：地点/地址/场馆/主办地）
        area = "广州（见PDF）"
        m_loc = re.search(r"(地点|地址|场馆)[:：]\s*([^。；;]{4,40})", block)
        if m_loc:
            area = norm(m_loc.group(2))

        # 取一个“像标题”的名字：优先块的前半段
        name = block[:40]
        # 若块里有“：”，取冒号后更像标题
        if "：" in block[:30]:
            name = block.split("：", 1)[1][:40]
        name = norm(name)

        if looks_bad(name):
            return

        tags = ["官方清单", "PDF解析"]
        if "展" in block or "展览" in block: tags.append("展览")
        if "音乐" in block or "演唱" in block or "音乐会" in block: tags.append("音乐")
        if "戏剧" in block or "话剧" in block or "舞台" in block: tags.append("戏剧")
        if "亲子" in block or "儿童" in block: tags.append("亲子")
        if any(k in block for k in ["花", "花期", "赏花", "梅", "荷", "樱"]): tags.append("看花")

        events.append(make_item(
            type="event",
            name=name,
            area=area,
            date=norm(date_hint),
            timeHint=norm(time_hint),
            cost="low",
            reservation="maybe",
            tags=tags,
            transitEase=3, transferComplexity=3, timeMin=80,
            intensity="low",
            crowdRisk=3, checkin=1,
            openHoursHint="以PDF清单/对应活动页面为准（可能需要预约/购票）",
            notes=block[:220],   # 把关键信息片段带上，足够你筛选
            link=source_pdf,
            source="wglj.gz.gov.cn/pdf"
        ))

    # 以“出现日期行”作为新块起点
    for ln in lines:
        if date_pat.search(ln):
            flush_buf(buf)
            buf = [ln]
        else:
            # 把相关信息续到当前块
            if buf:
                buf.append(ln)
            else:
                # 未开始但也许是标题行
                if len(ln) > 10:
                    buf = [ln]

        # 控制块长度，避免合并过长
        if len(buf) >= 6:
            flush_buf(buf)
            buf = []

    flush_buf(buf)
    return events




def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def looks_bad(title: str) -> bool:
    t = norm(title)
    if any(k in t for k in BAD_KEYWORDS):
        # 如果只是“约会”等软词，不直接过滤
        if any(k in t for k in SOFT_BAD_KEYWORDS) and not any(k in t for k in BAD_KEYWORDS):
            return False
        return True
    return False

def guess_cost(text: str) -> str:
    # 粗略票价判断
    m = re.search(r"([¥￥]?\s*\d+(\.\d+)?)\s*元", text)
    if not m:
        return "mid"
    try:
        v = float(re.sub(r"[^\d.]", "", m.group(1)))
        if v <= 60: return "low"
        if v <= 160: return "mid"
        return "high"
    except Exception:
        return "mid"

def make_item(**kw) -> Dict[str, Any]:
    # 给缺省字段补齐（跟你前端 schema 对齐）
    base = {
        "type": kw.get("type", "event"),
        "name": kw.get("name", ""),
        "area": kw.get("area", "广州"),
        "date": kw.get("date", ""),
        "timeHint": kw.get("timeHint", ""),
        "cost": kw.get("cost", "mid"),
        "reservation": kw.get("reservation", "maybe"),
        "tags": kw.get("tags", []),
        "transitEase": kw.get("transitEase", 3),
        "transferComplexity": kw.get("transferComplexity", 3),
        "timeMin": kw.get("timeMin", 80),
        "intensity": kw.get("intensity", "low"),
        "crowdRisk": kw.get("crowdRisk", 3),
        "checkin": kw.get("checkin", 2),
        "weatherFit": kw.get("weatherFit", {"rain": True, "sun": True, "cold": True}),
        "seasonFit": kw.get("seasonFit", {"spring": True, "summer": True, "autumn": True, "winter": True}),
        "mosquito": kw.get("mosquito", 1),
        "toiletSupply": kw.get("toiletSupply", 3),
        "lighting": kw.get("lighting", 4),
        "openHoursHint": kw.get("openHoursHint", "以官方公告/详情页为准"),
        "notes": kw.get("notes", ""),
        "link": kw.get("link", ""),
        "source": kw.get("source", "unknown"),
    }
    return base

# ---------- 1) 文旅局活动排期索引：抓多条入口 ----------
def parse_wglj_schedule_index(limit: int = 25) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        html = http_get(WGLJ_SCHEDULE_INDEX)
        soup = BeautifulSoup(html, "html.parser")
        # 索引页里一般是文章列表链接，尽量宽松抓取
        anchors = soup.select("a")
        for a in anchors:
            title = norm(a.get_text())
            href = a.get("href") or ""
            if not title or not href:
                continue
            if "活动" not in title and "排期" not in title and "精选" not in title:
                continue
            if href.startswith("/"):
                href = "https://wglj.gz.gov.cn" + href
            if not href.startswith("http"):
                continue

            # 不解析附件内容，先把“官方清单/排期”作为可点开的来源条目
            items.append(make_item(
                type="event",
                name=title,
                area="广州（全市）",
                cost="low",
                reservation="maybe",
                tags=["官方资讯", "活动清单"],
                transitEase=3, transferComplexity=3, timeMin=75,
                intensity="low",
                crowdRisk=3, checkin=1,
                openHoursHint="打开来源页查看附件/具体活动",
                notes="来自广州市文旅局活动排期索引，可用来挑本周/本月节目与展览。",
                link=href,
                source="wglj.gz.gov.cn/hdpq"
            ))
            # ===== 新增：进入该页面，找 PDF 并解析 =====
try:
    pdf_links = find_pdf_links_in_page(href)

    for pdf_url in pdf_links[:3]:  # 每个页面最多解析前 3 个 PDF，防止过慢
        try:
            pdf_bytes = http_get_bytes(pdf_url)
            text = extract_pdf_text(pdf_bytes, max_pages=12)

            # 如果 PDF 是图片扫描版，直接文本会很少，先跳过
            if len(norm(text)) < 80:
                continue

            events = split_events_from_pdf_text(
                text,
                source_pdf=pdf_url
            )

            items.extend(events)

        except Exception as e2:
            print(f"[WGLJ pdf] failed {pdf_url}: {e2}")

except Exception as e1:
    print(f"[WGLJ page->pdf] failed {href}: {e1}")

            if len(items) >= limit:
                break
    except Exception as e:
        print(f"[WGLJ index] failed: {e}")
    return items

# ---------- 2) 豆瓣同城：分类页 + 分页 ----------
def extract_douban_event_links(list_html: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(list_html, "html.parser")

    pairs: List[Tuple[str, str]] = []

    # 方式A：抓 href 含 /event/ 的链接（豆瓣可能是 www.douban.com/event/xxxx）
    for a in soup.select("a"):
        href = a.get("href") or ""
        text = norm(a.get_text())
        if not text:
            continue
        if "douban.com/event/" in href or re.search(r"/event/\d+", href):
            pairs.append((text, href))

    # 去重
    seen = set()
    uniq = []
    for t, h in pairs:
        key = (t, h)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((t, h))
    return uniq

def parse_douban_list(category_name: str, base_url: str, pages: int = 6, page_step: int = 10) -> List[Dict[str, Any]]:
    """
    pages: 取多少页分页；豆瓣常见 start=0/10/20... 或 0/20/40...
    这里同时尝试 10 的步长，兼容不同列表。
    """
    out: List[Dict[str, Any]] = []
    for i in range(pages):
        start = i * page_step
        url = base_url if start == 0 else f"{base_url}?start={start}"
        try:
            html = http_get(url)
        except Exception as e:
            print(f"[Douban list] failed {url}: {e}")
            continue

        links = extract_douban_event_links(html)
        for title, href in links:
            if looks_bad(title):
                continue

            # 简单从列表页文本里估一个 cost（不打开详情页也能有量）
            cost = "mid"
            if "免费" in html:
                # 这只是粗略，不强求准确
                pass

            out.append(make_item(
                type="event",
                name=title,
                area="广州（见详情）",
                date="",
                timeHint="",
                cost=cost,
                reservation="maybe",
                tags=["同城活动", category_name],
                transitEase=3, transferComplexity=3, timeMin=85,
                intensity="low",
                crowdRisk=3, checkin=2,
                openHoursHint="以活动详情页为准（可能需购票/预约）",
                notes="来自豆瓣同城列表，用于补充当周可去的演出/展览/活动。",
                link=href,
                source=category_name
            ))

    return out

# ---------- 3) 广东省博物馆：最新活动/活动列表 ----------
def parse_gdmuseum_activities(limit: int = 20) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        html = http_get(GDMUSEUM_HOME)
        soup = BeautifulSoup(html, "html.parser")
        # 首页“最新活动”区域：尽量宽松抓取链接+标题
        for a in soup.select("a"):
            title = norm(a.get_text())
            href = a.get("href") or ""
            if not title or not href:
                continue
            if "活动" not in title and "工坊" not in title and "迎新" not in title:
                continue
            if href.startswith("/"):
                href = "https://www.gdmuseum.com" + href
            if not href.startswith("http"):
                continue
            if looks_bad(title):
                continue

            items.append(make_item(
                type="event",
                name=title,
                area="天河·广东省博物馆",
                cost="low",
                reservation="maybe",
                tags=["官方场馆", "粤博", "室内"],
                transitEase=3, transferComplexity=3, timeMin=80,
                intensity="low",
                crowdRisk=3, checkin=1,
                weatherFit={"rain": True, "sun": True, "cold": True},
                openHoursHint="以粤博公告/活动页为准（通常需预约入馆）",
                notes="官方场馆活动，雨天/暴晒天友好。",
                link=href,
                source="gdmuseum.com/home"
            ))
            if len(items) >= limit:
                break
    except Exception as e:
        print(f"[GDMuseum] failed: {e}")

    # 再抓“活动列表页”（可能比首页更多）
    try:
        html = http_get(GDMUSEUM_ACTIVITY_LIST)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a"):
            title = norm(a.get_text())
            href = a.get("href") or ""
            if not title or not href:
                continue
            if looks_bad(title):
                continue
            if href.startswith("/"):
                href = "https://www.gdmuseum.com" + href
            if "gdmuseum.com" not in href:
                continue
            # 避免把导航等也抓进来：标题过短/过泛就跳过
            if len(title) < 6:
                continue

            items.append(make_item(
                type="event",
                name=title,
                area="天河·广东省博物馆",
                cost="low",
                reservation="maybe",
                tags=["官方场馆", "粤博", "室内"],
                transitEase=3, transferComplexity=3, timeMin=80,
                intensity="low",
                crowdRisk=3, checkin=1,
                openHoursHint="以活动页为准（通常需预约入馆）",
                notes="来自粤博活动列表页。",
                link=href,
                source="gdmuseum.com/col108"
            ))
            if len(items) >= limit * 2:
                break
    except Exception as e:
        print(f"[GDMuseum list] failed: {e}")

    return items

# ---------- 4) 广州博物馆：展览列表/预告 ----------
def parse_gzmuseum_exhibitions(limit: int = 20) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for url, source in [(GZMUSEUM_EXHIBITION_LIST, "guangzhoumuseum.cn/exhibition"),
                        (GZMUSEUM_EXHIBITION_PRE, "guangzhoumuseum.cn/pre")]:
        try:
            html = http_get(url)
            soup = BeautifulSoup(html, "html.parser")
            # 该站点有明确的展览条目标题，宽松抓取“分享/收藏”附近的标题会更复杂，
            # 这里用：抓取页面中明显是展览名称的链接或标题块（尽量宽松）
            # 简化策略：抓所有 a 的文本，过滤长度，拼上站点 url
            for a in soup.select("a"):
                title = norm(a.get_text())
                href = a.get("href") or ""
                if not title:
                    continue
                # 过滤导航与无意义短词
                if len(title) < 6:
                    continue
                if "首页" in title or "概况" in title or "动态" in title:
                    continue
                if looks_bad(title):
                    continue

                # href 若是相对路径，补全
                if href.startswith("/"):
                    href = "https://www.guangzhoumuseum.cn" + href
                if href and not href.startswith("http"):
                    # 有些是 javascript:void(0) 之类
                    href = ""

                items.append(make_item(
                    type="event",
                    name=title,
                    area="广州博物馆（越秀/镇海楼等馆区）",
                    cost="low",
                    reservation="maybe",
                    tags=["官方场馆", "博物馆", "展览", "室内"],
                    transitEase=3, transferComplexity=3, timeMin=85,
                    intensity="low",
                    crowdRisk=3, checkin=1,
                    openHoursHint="以广州博物馆官网展览页面为准",
                    notes="官方展览信息（适合雨天/暴晒天）。",
                    link=href or url,
                    source=source
                ))
                if len(items) >= limit:
                    break
        except Exception as e:
            print(f"[GZ Museum] failed {url}: {e}")
    return items

def dedupe(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for it in items:
        name = it.get("name", "")
        link = it.get("link", "")
        key = (name, link)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def main():
    items: List[Dict[str, Any]] = []

    # 文旅局活动排期索引（多条）
    items.extend(parse_wglj_schedule_index(limit=25))

    # 豆瓣同城：分类+分页
    for source_name, url in DOUBAN_PAGES:
        # pages=8 约抓 8页；page_step=10 更容易拿到更多
        items.extend(parse_douban_list(source_name, url, pages=8, page_step=10))

    # 粤博
    items.extend(parse_gdmuseum_activities(limit=25))

    # 广州博物馆
    items.extend(parse_gzmuseum_exhibitions(limit=20))

    # 过滤二次（防漏）+ 去重
    items = [it for it in items if it.get("name") and not looks_bad(it["name"])]
    items = dedupe(items)

    # 限制最终条目数，避免太大（你可以调）
    MAX_ITEMS = 220
    items = items[:MAX_ITEMS]

    out = {
        "items": items,
        "meta": {
            "generatedAt": now_cn_iso(),
            "sources": [WGLJ_SCHEDULE_INDEX] + [u for _, u in DOUBAN_PAGES] + [GDMUSEUM_HOME, GDMUSEUM_ACTIVITY_LIST, GZMUSEUM_EXHIBITION_LIST],
            "notes": f"自动生成广州活动数据，合并官方+同城聚合，已过滤探店打卡关键词。max={MAX_ITEMS}"
        }
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Generated data.json with {len(items)} items.")

if __name__ == "__main__":
    main()
