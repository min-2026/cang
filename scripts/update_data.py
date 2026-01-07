# scripts/update_data.py
# -*- coding: utf-8 -*-

import json
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ====== 你可随时扩展的信息源 ======
WGLJ_HOME = "https://wglj.gz.gov.cn/"  # 广州市文化广电旅游局首页（会滚动最新资讯）:contentReference[oaicite:3]{index=3}
DOUBAN_WEEK = "https://guangzhou.douban.com/events/week-all"  # 最近一周活动 :contentReference[oaicite:4]{index=4}
DOUBAN_FUTURE = "https://guangzhou.douban.com/events/future-all"  # 近期活动 :contentReference[oaicite:5]{index=5}

# 你说不爱“打卡探店”，这里做自动过滤（你也可增减）
BAD_KEYWORDS = [
    "打卡", "探店", "网红", "拍照", "出片", "双人特惠", "约拍",
    "团购", "必打卡", "小红书", "种草"
]

def now_china_iso() -> str:
    # 中国时间 ISO（+08:00）
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")

def http_get(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text

def looks_like_bad(title: str) -> bool:
    t = (title or "").strip()
    return any(k in t for k in BAD_KEYWORDS)

def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def parse_wglj_monthly_from_home() -> List[Dict[str, Any]]:
    """
    从文旅局首页抓“活动精选月刊”等条目（不解析PDF内容，只保留链接+标题）
    稳定性较高：首页有最新资讯列表。:contentReference[oaicite:6]{index=6}
    """
    items: List[Dict[str, Any]] = []
    try:
        html = http_get(WGLJ_HOME)
        soup = BeautifulSoup(html, "html.parser")
        # 找到所有链接，筛选包含“活动精选”“月刊”的标题
        for a in soup.select("a"):
            text = normalize_space(a.get_text())
            href = a.get("href") or ""
            if not text or not href:
                continue
            if "活动精选" in text and ("月刊" in text or "月" in text):
                if href.startswith("/"):
                    href = "https://wglj.gz.gov.cn" + href
                # 生成一个 event 条目（偏资讯/清单）
                items.append({
                    "type": "event",
                    "name": text,
                    "area": "广州（全市）",
                    "date": "",  # 月刊是汇总，不一定有具体日期
                    "timeHint": "",
                    "cost": "low",
                    "reservation": "maybe",
                    "tags": ["官方资讯", "活动清单"],
                    "transitEase": 3,
                    "transferComplexity": 3,
                    "timeMin": 75,  # 对黄埔/萝岗用户按“全市出行”给个中性时间
                    "intensity": "low",
                    "crowdRisk": 3,
                    "checkin": 1,
                    "weatherFit": {"rain": True, "sun": True, "cold": True},
                    "seasonFit": {"spring": True, "summer": True, "autumn": True, "winter": True},
                    "mosquito": 1,
                    "toiletSupply": 3,
                    "lighting": 4,
                    "openHoursHint": "以官方公告/对应活动页面为准",
                    "notes": "官方发布的活动汇总清单，适合用来挑本周/本月节目与展览。",
                    "link": href,
                    "source": "wglj.gz.gov.cn"
                })
                # 只取前几条，避免重复太多
                if len(items) >= 3:
                    break
    except Exception as e:
        # 抓取失败就返回空，让系统继续用其他源
        print(f"[WGLJ] failed: {e}")
    return items

def parse_douban_events(list_url: str, source_name: str) -> List[Dict[str, Any]]:
    """
    抓取豆瓣同城列表页：提取 标题/时间/地点/费用/详情页链接
    页面结构可能会变化，但通常一段时间内稳定。:contentReference[oaicite:7]{index=7}
    """
    items: List[Dict[str, Any]] = []
    try:
        html = http_get(list_url)
        soup = BeautifulSoup(html, "html.parser")

        # 豆瓣活动列表：通常每个活动块里有标题链接
        # 我们尽量写“宽松选择器”：抓所有 href 含 /event/ 的链接
        links = []
        for a in soup.select("a"):
            href = a.get("href") or ""
            text = normalize_space(a.get_text())
            if "/event/" in href and text:
                links.append((text, href))

        # 去重（同名同链接）
        seen = set()
        uniq = []
        for t, h in links:
            key = (t, h)
            if key in seen:
                continue
            seen.add(key)
            uniq.append((t, h))

        # 只处理前 N 个，避免跑太久
        for title, href in uniq[:30]:
            if looks_like_bad(title):
                continue

            # 进入详情页拿 时间/地点/费用（更准）
            time_hint = ""
            area = "广州"
            cost = "mid"
            try:
                detail_html = http_get(href)
                dsoup = BeautifulSoup(detail_html, "html.parser")

                # 豆瓣详情页通常有“时间/地点/费用”等字段
                # 用关键词去找更稳
                text_all = normalize_space(dsoup.get_text(" "))
                # 粗略提取：不追求完美，够用即可
                # 时间：找“时间：”后的短片段
                m_time = re.search(r"时间[:：]\s*([^。|\n]{0,50})", text_all)
                if m_time:
                    time_hint = normalize_space(m_time.group(1))

                m_loc = re.search(r"地点[:：]\s*([^。|\n]{0,60})", text_all)
                if m_loc:
                    area = normalize_space(m_loc.group(1))
                    # 简化一下地点字段
                    if len(area) > 40:
                        area = area[:40] + "…"

                m_fee = re.search(r"(费用|票价)[:：]\s*([¥￥]?\s*\d+(\.\d+)?)", text_all)
                if m_fee:
                    fee_num = float(re.sub(r"[^\d.]", "", m_fee.group(2)))
                    if fee_num <= 60:
                        cost = "low"
                    elif fee_num <= 160:
                        cost = "mid"
                    else:
                        cost = "high"
                else:
                    cost = "mid"
            except Exception:
                pass

            items.append({
                "type": "event",
                "name": title,
                "area": area,
                "date": "",  # 豆瓣时间可能是范围/多天，不强制拆 date
                "timeHint": time_hint,
                "cost": cost,
                "reservation": "maybe",
                "tags": ["活动", "同城", "不以拍照为主"],
                "transitEase": 3,
                "transferComplexity": 3,
                "timeMin": 80,  # 全市活动中性估计（你也可后续做“按区县估时”）
                "intensity": "low",
                "crowdRisk": 3,
                "checkin": 2,
                "weatherFit": {"rain": True, "sun": True, "cold": True},
                "seasonFit": {"spring": True, "summer": True, "autumn": True, "winter": True},
                "mosquito": 1,
                "toiletSupply": 3,
                "lighting": 4,
                "openHoursHint": "以活动详情页为准（可能需要预约/购票）",
                "notes": "来自同城活动聚合，适合补充“当周可去”的演出/展览/活动。",
                "link": href,
                "source": source_name
            })

        # 再做一轮关键词过滤（防漏）
        items = [it for it in items if not looks_like_bad(it["name"])]
    except Exception as e:
        print(f"[Douban] failed: {e}")

    return items

def main() -> None:
    items: List[Dict[str, Any]] = []

    # 1) 官方（稳定）
    items.extend(parse_wglj_monthly_from_home())

    # 2) 豆瓣（补充当周/近期活动）
    items.extend(parse_douban_events(DOUBAN_WEEK, "douban.com/week-all"))
    items.extend(parse_douban_events(DOUBAN_FUTURE, "douban.com/future-all"))

    # 去重：按 (name, link)
    seen = set()
    uniq_items = []
    for it in items:
        key = (it.get("name"), it.get("link"))
        if key in seen:
            continue
        seen.add(key)
        uniq_items.append(it)

    out = {
        "items": uniq_items,
        "meta": {
            "generatedAt": now_china_iso(),
            "sources": [
                WGLJ_HOME,
                DOUBAN_WEEK,
                DOUBAN_FUTURE,
            ],
            "notes": "自动生成的广州活动数据（已过滤打卡/探店关键词）。"
        }
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"Generated data.json with {len(uniq_items)} items.")

if __name__ == "__main__":
    main()
