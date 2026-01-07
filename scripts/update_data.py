# scripts/update_data.py
# -*- coding: utf-8 -*-

import json
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

import fitz  # PyMuPDF


HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

TZ_CN = timezone(timedelta(hours=8))

# ===== 信息源（公开、结构相对稳定）=====
WGLJ_SCHEDULE_INDEX = "https://wglj.gz.gov.cn/ztmb/gzhyn/hdpq/mindex.html"  # 文旅局活动排期索引

DOUBAN_BASE = "https://guangzhou.douban.com/events/"
DOUBAN_PAGES = [
    ("douban/week-all",        DOUBAN_BASE + "week-all"),
    ("douban/week-music",      DOUBAN_BASE + "week-music"),
    ("douban/week-drama",      DOUBAN_BASE + "week-drama"),
    ("douban/week-exhibition", DOUBAN_BASE + "week-exhibition"),
    ("douban/week-course",     DOUBAN_BASE + "week-course"),
]

# 你原文件里这里写成了 "s://..."，会导致 requests 直接报错；我已修正为 https://  :contentReference[oaicite:1]{index=1}
GDMUSEUM_HOME = "https://www.gdmuseum.com/"
GDMUSEUM_ACTIVITY_LIST = "https://www.gdmuseum.com/col108/list"

GZMUSEUM_EXHIBITION_LIST = "https://www.guangzhoumuseum.cn/website_cn/Web/Exhibition/Exhibition.aspx"
GZMUSEUM_EXHIBITION_PRE = "https://www.guangzhoumuseum.cn/website_cn/Web/Dynamic/Exhibition.aspx"


# ===== 过滤规则（不爱探店打卡）=====
BAD_KEYWORDS = [
    "探店", "网红", "出片", "约拍", "拍照打卡", "打卡地", "打卡点",
    "团购", "双人特惠", "必打卡", "种草", "咖啡探店"
]
SOFT_BAD_KEYWORDS = ["约会", "情侣"]


def now_cn_iso() -> str:
    return datetime.now(TZ_CN).isoformat(timespec="seconds")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def looks_bad(title: str) -> bool:
    t = norm(title)
    if any(k in t for k in BAD_KEYWORDS):
        # 软词不做强过滤（防误杀）
        if any(k in t for k in SOFT_BAD_KEYWORDS) and not any(k in t for k in BAD_KEYWORDS):
            return False
        return True
    return False


def http_get(url: str, timeout: int = 25) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def http_get_bytes(url: str, timeout: int = 35) -> bytes:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content


def extract_pdf_text(pdf_bytes: bytes, max_pages: int = 12) -> str:
    """
    从 PDF 提取文本（优先文本层，不做 OCR）。
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        texts = []
        pages = min(len(doc), max_pages)
        for i in range(pages):
            page = doc.load_page(i)
            t = page.get_text("text") or ""
            texts.append(t)
        return "\n".join(texts)
    finally:
        doc.close()


def find_pdf_links_in_page(page_url: str) -> List[str]:
    """
    打开文旅局页面，找出 PDF 附件链接（宽松匹配）。
    """
    html = http_get(page_url)
    soup = BeautifulSoup(html, "html.parser")
    pdfs: List[str] = []

    for a in soup.select("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue

        low = href.lower()
        if low.endswith(".pdf") or ".pdf?" in low:
            pdfs.append(urljoin(page_url, href))
            continue

        # 一些附件链接可能不是以 .pdf 结尾（例如下载接口），先收集
        if any(x in low for x in ["download", "attach", "file", "附件"]):
            pdfs.append(urljoin(page_url, href))

    # 去重
    seen = set()
    out: List[str] = []
    for u in pdfs:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def make_item(**kw) -> Dict[str, Any]:
    """
    给缺省字段补齐（和你前端 schema 对齐）
    """
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


def split_events_from_pdf_text(pdf_text: str, source_pdf: str) -> List[Dict[str, Any]]:
    """
    把 PDF 文本“粗拆”成活动条目。先求可用：让你不用下载也能在网页里看到关键信息。
    """
    lines = [norm(x) for x in pdf_text.splitlines()]
    lines = [x for x in lines if x and len(x) >= 4]

    date_pat = re.compile(r"(\d{4}年)?\s*\d{1,2}\s*[月/.\-]\s*\d{1,2}\s*(日)?")
    time_pat = re.compile(r"(\d{1,2}:\d{2})\s*[-~—–]\s*(\d{1,2}:\d{2})")

    events: List[Dict[str, Any]] = []
    buf: List[str] = []

    def flush_buf(buf_lines: List[str]) -> None:
        if not buf_lines:
            return
        block = " ".join(buf_lines)
        if len(block) < 16:
            return

        m_time = time_pat.search(block)
        time_hint = m_time.group(0) if m_time else ""

        m_date = date_pat.search(block)
        date_hint = m_date.group(0) if m_date else ""

        area = "广州（见PDF）"
        m_loc = re.search(r"(地点|地址|场馆)[:：]\s*([^。；;]{4,40})", block)
        if m_loc:
            area = norm(m_loc.group(2))

        name = norm(block[:40])
        if "：" in block[:30]:
            name = norm(block.split("：", 1)[1][:40])

        if looks_bad(name):
            return

        tags = ["官方清单", "PDF解析"]
        if any(k in block for k in ["展览", "展"]):
            tags.append("展览")
        if any(k in block for k in ["音乐会", "演唱", "音乐"]):
            tags.append("音乐")
        if any(k in block for k in ["戏剧", "话剧", "舞台"]):
            tags.append("戏剧")
        if any(k in block for k in ["亲子", "儿童"]):
            tags.append("亲子")
        if any(k in block for k in ["花期", "赏花", "樱", "梅", "荷", "红叶"]):
            tags.append("看花")

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
            notes=block[:260],
            link=source_pdf,
            source="wglj.gz.gov.cn/pdf"
        ))

    for ln in lines:
        if date_pat.search(ln):
            flush_buf(buf)
            buf = [ln]
        else:
            if buf:
                buf.append(ln)
            else:
                if len(ln) > 10:
                    buf = [ln]

        # 控制块长度，避免把太多行合并成一个活动
        if len(buf) >= 6:
            flush_buf(buf)
            buf = []

    flush_buf(buf)
    return events


# ---------- 1) 文旅局活动排期索引：入口页 + PDF 解析 ----------
def parse_wglj_schedule_index(limit: int = 25) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    try:
        html = http_get(WGLJ_SCHEDULE_INDEX)
        soup = BeautifulSoup(html, "html.parser")

        anchors = soup.select("a")
        for a in anchors:
            title = norm(a.get_text())
            href = (a.get("href") or "").strip()

            if not title or not href:
                continue
            if "活动" not in title and "排期" not in title and "精选" not in title:
                continue

            if href.startswith("/"):
                href = "https://wglj.gz.gov.cn" + href
            if not href.startswith("http"):
                continue

            # (A) 入口页保留一条（兜底：就算 PDF 解析挂了也能点进去）
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
                openHoursHint="打开来源页查看附件/具体活动（PDF）",
                notes="来自广州市文旅局活动排期索引。",
                link=href,
                source="wglj.gz.gov.cn/hdpq"
            ))

            # (B) 进入入口页：找 PDF → 下载 → 解析 → 拆条目
            pdf_links: List[str] = []
            try:
                pdf_links = find_pdf_links_in_page(href)
            except Exception as e_find:
                print(f"[WGLJ page->pdf] failed to find pdfs {href}: {e_find}")

            for pdf_url in pdf_links[:3]:  # 每个入口页最多解析 3 个 PDF，避免过慢
                try:
                    pdf_bytes = http_get_bytes(pdf_url)
                    text = extract_pdf_text(pdf_bytes, max_pages=12)

                    # 扫描版/图片版 PDF 会提不到字，先跳过（需要 OCR 才能做）
                    if len(norm(text)) < 80:
                        continue

                    events = split_events_from_pdf_text(text, source_pdf=pdf_url)
                    if events:
                        items.extend(events)

                except Exception as e_pdf:
                    print(f"[WGLJ pdf] failed {pdf_url}: {e_pdf}")

            if len(items) >= limit:
                break

    except Exception as e:
        print(f"[WGLJ index] failed: {e}")

    return items


# ---------- 2) 豆瓣同城：分类页 + 分页 ----------
def extract_douban_event_links(list_html: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(list_html, "html.parser")
    pairs: List[Tuple[str, str]] = []

    for a in soup.select("a"):
        href = a.get("href") or ""
        text = norm(a.get_text())
        if not text:
            continue
        if "douban.com/event/" in href or re.search(r"/event/\d+", href):
            pairs.append((text, href))

    seen = set()
    uniq: List[Tuple[str, str]] = []
    for t, h in pairs:
        key = (t, h)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((t, h))
    return uniq


def parse_douban_list(category_name: str, base_url: str, pages: int = 8, page_step: int = 10) -> List[Dict[str, Any]]:
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

            out.append(make_item(
                type="event",
                name=title,
                area="广州（见详情）",
                date="",
                timeHint="",
                cost="mid",
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


# ---------- 3) 广东省博物馆：活动 ----------
def parse_gdmuseum_activities(limit: int = 25) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []

    try:
        html = http_get(GDMUSEUM_HOME)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a"):
            title = norm(a.get_text())
            href = (a.get("href") or "").strip()
            if not title or not href:
                continue
            if "活动" not in title and "工坊" not in title and "迎新" not in title:
                continue
            if looks_bad(title):
                continue
            if href.startswith("/"):
                href = "https://www.gdmuseum.com" + href
            if not href.startswith("http"):
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

    # 活动列表页（补充）
    try:
        html = http_get(GDMUSEUM_ACTIVITY_LIST)
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a"):
            title = norm(a.get_text())
            href = (a.get("href") or "").strip()
            if not title or not href:
                continue
            if looks_bad(title):
                continue
            if len(title) < 6:
                continue
            if href.startswith("/"):
                href = "https://www.gdmuseum.com" + href
            if "gdmuseum.com" not in href:
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


# ---------- 4) 广州博物馆：展览 ----------
def parse_gzmuseum_exhibitions(limit: int = 20) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for url, source in [
        (GZMUSEUM_EXHIBITION_LIST, "guangzhoumuseum.cn/exhibition"),
        (GZMUSEUM_EXHIBITION_PRE, "guangzhoumuseum.cn/pre"),
    ]:
        try:
            html = http_get(url)
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select("a"):
                title = norm(a.get_text())
                href = (a.get("href") or "").strip()
                if not title:
                    continue
                if len(title) < 6:
                    continue
                if "首页" in title or "概况" in title or "动态" in title:
                    continue
                if looks_bad(title):
                    continue

                if href.startswith("/"):
                    href = "https://www.guangzhoumuseum.cn" + href
                if href and not href.startswith("http"):
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
    out: List[Dict[str, Any]] = []
    for it in items:
        key = (it.get("name", ""), it.get("link", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def main() -> None:
    items: List[Dict[str, Any]] = []

    # 文旅局（入口页 + PDF解析条目）
    items.extend(parse_wglj_schedule_index(limit=25))

    # 豆瓣同城
    for source_name, url in DOUBAN_PAGES:
        items.extend(parse_douban_list(source_name, url, pages=8, page_step=10))

    # 粤博
    items.extend(parse_gdmuseum_activities(limit=25))

    # 广州博物馆
    items.extend(parse_gzmuseum_exhibitions(limit=20))

    # 过滤 + 去重
    items = [it for it in items if it.get("name") and not looks_bad(it["name"])]
    items = dedupe(items)

    # 防止过大
    MAX_ITEMS = 260
    items = items[:MAX_ITEMS]

    out = {
        "items": items,
        "meta": {
            "generatedAt": now_cn_iso(),
            "sources": [WGLJ_SCHEDULE_INDEX] + [u for _, u in DOUBAN_PAGES] + [
                GDMUSEUM_HOME, GDMUSEUM_ACTIVITY_LIST, GZMUSEUM_EXHIBITION_LIST, GZMUSEUM_EXHIBITION_PRE
            ],
            "notes": f"自动生成广州活动数据（含文旅局PDF文本解析）。max={MAX_ITEMS}"
        }
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Generated data.json with {len(items)} items.")


if __name__ == "__main__":
    main()
