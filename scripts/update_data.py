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
GDMU
