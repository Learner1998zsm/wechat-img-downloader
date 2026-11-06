#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号原图下载器
================
批量提取微信公众号文章正文配图，并自动替换为原图地址后下载。

原理：
  1. 文章正文里的 <img> 使用 data-src（懒加载），尺寸段通常为 /640；
  2. 微信图床 mmbiz.qpic.cn 的尺寸段 /640 返回压缩图（约 1080px 宽），
     把它替换成 /0 即可拿到原始分辨率的原图；
  3. 下载时带上 Referer 与浏览器 UA，避免被防盗链拦截。

用法：
  双击（无参数）启动图形界面；
  命令行： python wx_img_downloader.py --cli <文章链接...> -o 输出目录
"""

from __future__ import annotations

import argparse
import base64
import html as html_lib
import json
import os
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

APP_NAME = "公众号原图下载器"
APP_VERSION = "1.1"
CHINA_TZ = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

IMAGE_HOSTS = ("qpic.cn", "qlogo.cn")
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"BM": "bmp",
}


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

class StopRequested(Exception):
    pass


class ArticleError(Exception):
    pass


class _Retry(Exception):
    pass


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def safe_name(name: str, maxlen: int = 60) -> str:
    """把标题变成合法的 Windows 文件夹名。"""
    name = html_lib.unescape(name or "")
    name = name.replace("\u200b", "").replace("\xa0", " ")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name)
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return ""
    if name.upper().split(".")[0] in {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        name = "_" + name
    return name[:maxlen].strip(" .") or ""


def brief_url(url: str, limit: int = 72) -> str:
    url = url or ""
    return url if len(url) <= limit else url[: limit - 1] + "…"


def _sleep(seconds: float, stop: Optional[threading.Event]) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if stop is not None and stop.is_set():
            return False
        time.sleep(min(0.1, max(0.0, end - time.time())))
    return True


_session_local = threading.local()


def get_session():
    """每个线程一个 Session，避免并发共享连接池。"""
    if requests is None:
        raise ArticleError("缺少 requests 库，请先执行：pip install requests")
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(BASE_HEADERS)
        _session_local.session = session
    return session


# --------------------------------------------------------------------------
# 链接解析与归一化
# --------------------------------------------------------------------------

_ARTICLE_RE = re.compile(r"(?:https?://)?mp\.weixin\.qq\.com/[^\s\"'<>]+", re.I)
_TRAILING = ".,;:!?)]}>\"'，。；：！？）】》、”’"


def extract_article_urls(text: str) -> list[str]:
    """从任意文本里提取公众号文章链接，去重并保持顺序。"""
    text = html_lib.unescape(text or "")
    urls: list[str] = []
    seen: set[str] = set()
    for match in _ARTICLE_RE.findall(text):
        url = match.strip().rstrip(_TRAILING)
        if not url:
            continue
        if not url.lower().startswith("http"):
            url = "https://" + url
        url = url.split("#", 1)[0]
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in IMAGE_HOSTS)


def normalize_image_url(raw: str) -> Optional[str]:
    """把文章里的图片地址变为原图地址。

    - http -> https
    - 末尾尺寸段（640 / 1080 / 200...）统一替换为 0（原图）
    - 去掉 tp=webp、wx_lazy、wxfrom 等无关参数，只保留 wx_fmt
    """
    if not raw:
        return None
    url = html_lib.unescape(raw).strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("http://"):
        url = "https://" + url[7:]
    if not url.startswith("https://"):
        return None

    parts = urlsplit(url)
    if not _host_allowed(parts.hostname or ""):
        return None

    path = re.sub(r"/(\d{1,6})$", "/0", parts.path)

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    fmt = (query.get("wx_fmt") or "").lower()
    if fmt not in IMAGE_EXTS:
        m = re.search(r"(?:mmbiz|sz_mmbiz)_(jpg|jpeg|png|gif|webp)", path, re.I)
        if m:
            fmt = m.group(1).lower()
    if fmt == "jpg":
        fmt = "jpeg"
    new_query = urlencode({"wx_fmt": fmt}) if fmt in IMAGE_EXTS else ""
    return urlunsplit(("https", parts.netloc.lower(), path, new_query, ""))


def is_gif_url(url: str) -> bool:
    return bool(re.search(r"\.gif(?:$|\?)", url, re.I) or "wx_fmt=gif" in url.lower())


# --------------------------------------------------------------------------
# 文章解析
# --------------------------------------------------------------------------

class _ContentParser(HTMLParser):
    """只收集正文容器 #js_content 内的 <img> 标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_content = False
        self.found = False
        self.depth = 0
        self.images: list[dict] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag == "div":
            if data.get("id") == "js_content":
                self.in_content = True
                self.found = True
                self.depth = 1
                return
            if self.in_content:
                self.depth += 1
        elif tag == "img" and self.in_content:
            self.images.append(data)

    def handle_startendtag(self, tag: str, attrs) -> None:
        # <img ... /> 会被当作 startendtag
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.in_content:
            self.depth -= 1
            if self.depth <= 0:
                self.in_content = False


class _AllImagesParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[dict] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "img":
            self.images.append({k.lower(): (v or "") for k, v in attrs})

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)


def _js_unescape(text: str) -> str:
    escape_map = {
        "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v",
        "'": "'", '"': '"', "\\": "\\", "/": "/", "0": "\0",
    }

    def repl(match: re.Match) -> str:
        token = match.group(1)
        try:
            if token.startswith("x") and len(token) == 3:
                return chr(int(token[1:], 16))
            if token.startswith("u") and len(token) == 5:
                return chr(int(token[1:], 16))
        except ValueError:
            return token
        return escape_map.get(token, token)

    text = re.sub(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", repl, text)
    return html_lib.unescape(text).replace("\u200b", "").replace("\u00a0", " ")


def _extract_js_string(text: str, name: str, allow_bare: bool = False) -> str:
    """读取 `var name = '...'` / `var name = htmlDecode("...")` 里的字符串。"""
    prefix = r"(?:var\s+)?" if allow_bare else r"var\s+"
    match = re.search(rf"{prefix}{re.escape(name)}\s*=\s*", text)
    if not match:
        return ""
    i = match.end()
    if text.startswith("htmlDecode(", i):
        i += len("htmlDecode(")
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text) or text[i] not in "'\"":
        return ""
    quote = text[i]
    i += 1
    buf: list[str] = []
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            buf.append(text[i:i + 2])
            i += 2
            continue
        if ch == quote:
            break
        buf.append(ch)
        i += 1
    return _js_unescape("".join(buf)).strip()


def _meta_content(text: str, prop: str) -> str:
    for pattern in (
        rf'<meta[^>]+property=["\']{re.escape(prop)}["\'][^>]+content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{re.escape(prop)}["\']',
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return html_lib.unescape(match.group(1)).strip()
    return ""


def _extract_title(text: str, fallback: str = "") -> str:
    title = _extract_js_string(text, "msg_title")
    if not title:
        title = _meta_content(text, "og:title")
    if not title:
        match = re.search(r'id=["\']activity-name["\'][^>]*>(.*?)<', text, re.S)
        if match:
            title = re.sub(r"<[^>]+>", "", match.group(1))
            title = html_lib.unescape(title).strip()
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        if match:
            title = html_lib.unescape(match.group(1)).strip()
            title = re.sub(r"\s*[-|_]\s*微信公众平台\s*$", "", title)
    title = re.sub(r"\s+", " ", title or "").strip()
    return title or fallback


def _extract_account(text: str) -> str:
    account = _extract_js_string(text, "nickname")
    if not account:
        account = _meta_content(text, "og:article:author")
    if not account:
        match = re.search(r'id=["\']js_name["\'][^>]*>(.*?)<', text, re.S)
        if match:
            account = html_lib.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return account


def _extract_date(text: str) -> str:
    for name in ("ct", "create_time"):
        match = re.search(rf"var\s+{name}\s*=\s*[\"']?(\d{{9,11}})", text)
        if match:
            try:
                ts = int(match.group(1))
                if 1262304000 < ts < 4102444800:  # 2010 ~ 2100
                    return datetime.fromtimestamp(ts, CHINA_TZ).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                pass
    match = re.search(r"(?:ori_create_time|create_time)\s*:\s*['\"]?(\d{9,11})", text)
    if match:
        try:
            ts = int(match.group(1))
            if 1262304000 < ts < 4102444800:
                return datetime.fromtimestamp(ts, CHINA_TZ).strftime("%Y-%m-%d")
        except (ValueError, OSError):
            pass
    match = re.search(r"create_time\s*:\s*['\"](\d{4}-\d{2}-\d{2})", text)
    if match:
        return match.group(1)
    match = re.search(r'id=["\']publish_time["\'][^>]*>([^<]{4,30})<', text)
    if match:
        value = html_lib.unescape(match.group(1)).strip()
        if value:
            return value[:10]
    return ""


def _balanced_block(text: str, start: int) -> str:
    """从 start 位置的 [ 或 { 开始，截取配对的完整代码块。"""
    depth = 0
    quote = ""
    i = start
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        else:
            if ch in "'\"":
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return ""


def extract_picture_page_images(html_text: str) -> list[str]:
    """图片分享页（图集 / 轮播图）的图片存放在 picture_page_info_list 里。"""
    found: list[str] = []
    for key in ("picture_page_info_list", "picture_page_info"):
        match = re.search(rf"{re.escape(key)}\s*:\s*[\[{{]", html_text)
        if not match:
            continue
        block = _balanced_block(html_text, match.end() - 1)
        if not block:
            continue
        for raw in re.findall(r"cdn_url\s*:\s*['\"]([^'\"]+)['\"]", block):
            found.append(_js_unescape(raw))
    return found


def extract_images(html_text: str) -> list[str]:
    parser = _ContentParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass

    tags = parser.images
    if not parser.found:
        # 没有标准正文容器（例如某些被改造过的页面），退化为全页扫描
        fallback = _AllImagesParser()
        try:
            fallback.feed(html_text)
            fallback.close()
        except Exception:
            pass
        tags = fallback.images

    urls: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> bool:
        normalized = normalize_image_url(raw)
        if not normalized:
            return False
        if normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)
        return True

    # 图片分享页（图集/轮播）没有正文容器，只以图集列表为准，
    # 避免把封面、二维码也当成正文图；普通文章的正文里也会带这份清单，
    # 但内容与正文图片重复，不能直接使用。
    gallery = extract_picture_page_images(html_text)
    if gallery and not parser.found:
        for raw in gallery:
            add(raw)
        return urls

    for tag in tags:
        for key in ("data-src", "data-original", "src"):
            raw = tag.get(key) or ""
            if not raw or raw.startswith("data:"):
                continue
            if add(raw):
                break
    return urls


def _extract_link(text: str) -> str:
    link = _extract_js_string(text, "msg_link", allow_bare=True) or _meta_content(text, "og:url")
    if link and link.startswith("http") and "mp.weixin.qq.com" in link:
        return link
    return ""


@dataclass
class Article:
    title: str
    account: str = ""
    date: str = ""
    url: str = ""
    images: list[str] = field(default_factory=list)


def parse_article(html_text: str, url: str = "", fallback_title: str = "") -> Article:
    if not url.startswith("http"):
        url = _extract_link(html_text) or url
    return Article(
        title=_extract_title(html_text, fallback_title),
        account=_extract_account(html_text),
        date=_extract_date(html_text),
        url=url,
        images=extract_images(html_text),
    )


def decode_html_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


VERIFY_MARKERS = ("环境异常", "完成验证后即可继续访问", "请在微信客户端打开链接", "secitptpage")
GONE_MARKERS = ("该内容已被发布者删除", "此内容因违规无法查看", "该公众号已迁移", "链接已过期", "参数错误")


def fetch_article(url: str, cfg: "Config", stop: threading.Event) -> Article:
    session = get_session()
    response = None
    error = ""
    for attempt in range(2):
        if stop.is_set():
            raise StopRequested()
        try:
            headers = {"Cookie": cfg.cookie} if cfg.cookie else None
            response = session.get(url, headers=headers, timeout=cfg.timeout)
            if response.status_code == 200:
                break
            error = f"HTTP {response.status_code}"
            if response.status_code in (403, 404):
                break
        except requests.RequestException as exc:
            error = exc.__class__.__name__
        if attempt == 0 and not _sleep(1.2, stop):
            raise StopRequested()

    if response is None or response.status_code != 200:
        raise ArticleError(f"文章获取失败（{error or '无响应'}）")

    response.encoding = "utf-8"
    text = response.text

    if "js_content" not in text and "picture_page_info" not in text:
        if any(marker in text for marker in VERIFY_MARKERS):
            raise ArticleError(
                "触发微信环境验证：请用浏览器打开文章并「另存为」网页，"
                "再用「本地 HTML」按钮解析"
            )
        if any(marker in text for marker in GONE_MARKERS):
            raise ArticleError("文章已删除或无法访问")
        raise ArticleError("未找到正文，可能需要验证或登录；可尝试用「本地 HTML」方式")

    article = parse_article(text, url=response.url or url)
    if not article.images:
        article.images = []
    return article


# --------------------------------------------------------------------------
# 图片下载
# --------------------------------------------------------------------------

def sniff_ext(data: bytes) -> Optional[str]:
    for magic, ext in IMAGE_MAGIC.items():
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def download_image_bytes(
    url: str,
    cfg: "Config",
    stop: threading.Event,
    referer: str = "https://mp.weixin.qq.com/",
) -> bytes:
    session = get_session()
    headers = {
        "Referer": referer if referer.startswith("http") else "https://mp.weixin.qq.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    last = ""
    for attempt in range(1, max(1, cfg.retries) + 1):
        if stop.is_set():
            raise StopRequested()
        try:
            with session.get(url, headers=headers, timeout=cfg.timeout, stream=True) as response:
                if response.status_code != 200:
                    last = f"HTTP {response.status_code}"
                    if response.status_code in (403, 404):
                        break
                    raise _Retry(last)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_content(65536):
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise ArticleError("图片超过 64MB，已跳过")
                data = b"".join(chunks)
                if len(data) < 64:
                    raise _Retry("响应为空")
                if sniff_ext(data) is None:
                    raise _Retry("返回内容不是图片")
                return data
        except (_Retry, requests.RequestException) as exc:
            last = last or str(exc) or exc.__class__.__name__
            if attempt < cfg.retries and not _sleep(0.8 * attempt, stop):
                raise StopRequested()
    raise ArticleError(f"下载失败（{last}）")


def find_existing(stem: Path) -> Optional[Path]:
    if not stem.parent.is_dir():
        return None
    for candidate in stem.parent.glob(stem.name + ".*"):
        if candidate.suffix.lower().lstrip(".") in IMAGE_EXTS and candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------
# 下载任务
# --------------------------------------------------------------------------

@dataclass
class Config:
    out_dir: Path
    workers: int = 4
    subfolder: bool = True
    skip_existing: bool = True
    save_info: bool = True
    include_gif: bool = True
    timeout: float = 30.0
    retries: int = 3
    cookie: str = ""


@dataclass
class Source:
    url: str
    html: Optional[str] = None
    label: str = ""


@dataclass
class Stats:
    articles: int = 0
    ok_articles: int = 0
    failed_articles: int = 0
    images: int = 0
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    stopped: bool = False


class Downloader:
    """在后台线程中顺序处理文章，篇内并发下载图片。"""

    def __init__(self, sources: list[Source], cfg: Config,
                 emit: Callable[[str, object], None], stop: threading.Event):
        self.sources = sources
        self.cfg = cfg
        self.emit = emit
        self.stop = stop
        self.stats = Stats(articles=len(sources))

    # -- 事件 ---------------------------------------------------------------
    def _log(self, level: str, message: str) -> None:
        self.emit("log", (level, message))

    def _status(self, text: str) -> None:
        self.emit("status", text)

    def _progress(self, percent: float, saved: Optional[int] = None) -> None:
        percent = max(0.0, min(100.0, percent))
        self.emit("progress", (percent, saved))

    # -- 主流程 -------------------------------------------------------------
    def run(self) -> Stats:
        total = len(self.sources)
        if total == 0:
            self.stats.stopped = True
            return self.stats
        try:
            for index, source in enumerate(self.sources, start=1):
                if self.stop.is_set():
                    break
                self._status(f"第 {index}/{total} 篇 · 正在获取文章…")
                self._log("info", f"【{index}/{total}】{source.label or brief_url(source.url)}")
                started = time.time()
                try:
                    article = self._load(source)
                except StopRequested:
                    break
                except ArticleError as exc:
                    self.stats.failed_articles += 1
                    self._log("err", f"  失败：{exc}")
                    self._progress(100.0 * index / total, self.stats.saved)
                    continue
                except Exception as exc:  # 防御性兜底
                    self.stats.failed_articles += 1
                    self._log("err", f"  失败：{exc.__class__.__name__}: {exc}")
                    self._progress(100.0 * index / total, self.stats.saved)
                    continue

                if source.html is None:
                    self._log("dim", f"  标题：{article.title}")
                    meta = " · ".join(x for x in (article.account, article.date) if x)
                    if meta:
                        self._log("dim", f"  公众号：{meta}")

                images = [u for u in article.images if self.cfg.include_gif or not is_gif_url(u)]
                self.stats.images += len(images)
                if not images:
                    self.stats.ok_articles += 1
                    self._log("warn", "  未找到正文图片")
                    self._progress(100.0 * index / total, self.stats.saved)
                    continue

                try:
                    dest = self._prepare_dir(article, index)
                    if self.cfg.save_info:
                        self._write_info(dest, article, len(images))
                except OSError as exc:
                    self.stats.failed_articles += 1
                    self._log("err", f"  无法创建保存目录：{exc}")
                    self._progress(100.0 * index / total, self.stats.saved)
                    continue

                if self.cfg.subfolder:
                    self._log("ok", f"  找到 {len(images)} 张，保存到「{dest.name}」")
                else:
                    self._log("ok", f"  找到 {len(images)} 张，直接保存到输出目录")
                saved, skipped, failed = self._download_all(
                    images, dest, article, index, total
                )
                self.stats.saved += saved
                self.stats.skipped += skipped
                self.stats.failed += failed
                self.stats.ok_articles += 1
                level = "ok" if failed == 0 else "warn"
                self._log(
                    level,
                    f"  本篇文章完成：保存 {saved} 张，跳过 {skipped} 张，失败 {failed} 张"
                    f"（{time.time() - started:.1f}s）",
                )
                self._progress(100.0 * index / total, self.stats.saved)
                if index < total and not self.stop.is_set():
                    _sleep(0.8, self.stop)

            self.stats.stopped = self.stop.is_set()
            return self.stats
        finally:
            self._status("")

    def _load(self, source: Source) -> Article:
        if source.html is not None:
            return parse_article(source.html, url=source.url,
                                 fallback_title=Path(source.url).stem)
        return fetch_article(source.url, self.cfg, self.stop)

    def _prepare_dir(self, article: Article, index: int) -> Path:
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        if not self.cfg.subfolder:
            return self.cfg.out_dir
        parts = [article.date, article.title]
        folder = safe_name("_".join(p for p in parts if p), 70) or f"文章{index}"
        dest = self.cfg.out_dir / folder
        dest.mkdir(parents=True, exist_ok=True)
        return dest

    def _info_name(self, article: Article) -> str:
        if self.cfg.subfolder:
            return "文章信息.txt"
        return f"{safe_name(article.title, 30) or '未命名'}_信息.txt"

    def _write_info(self, dest: Path, article: Article, count: int) -> None:
        lines = [
            f"标题：{article.title}",
            f"公众号：{article.account}",
            f"发布时间：{article.date}",
            f"原文链接：{article.url}",
            f"图片数量：{count}",
            f"下载时间：{datetime.now(CHINA_TZ).strftime('%Y-%m-%d %H:%M:%S')}",
        ]
        try:
            (dest / self._info_name(article)).write_text(
                "\n".join(lines), encoding="utf-8-sig"
            )
        except OSError as exc:
            self._log("warn", f"  文章信息写入失败：{exc}")

    def _download_all(self, images: list[str], dest: Path, article: Article,
                      index: int, total: int) -> tuple[int, int, int]:
        n = len(images)
        width = max(2, len(str(n)))
        prefix = "" if self.cfg.subfolder else (safe_name(article.title, 24) + "_")
        results = {"saved": 0, "skipped": 0, "failed": 0}
        done = 0

        pool = ThreadPoolExecutor(max_workers=max(1, min(8, self.cfg.workers)))
        futures = {}
        try:
            for order, url in enumerate(images, start=1):
                if self.stop.is_set():
                    break
                stem = f"{prefix}{order:0{width}d}"
                futures[pool.submit(self._one_image, url, dest, stem, article.url)] = order

            for future in as_completed(futures):
                try:
                    state, message = future.result()
                except StopRequested:
                    state, message = "failed", "已停止"
                except ArticleError as exc:
                    state, message = "failed", str(exc)
                except Exception as exc:
                    state, message = "failed", f"{exc.__class__.__name__}: {exc}"

                results[state] = results.get(state, 0) + 1
                if state == "saved":
                    self._log("dim", f"  ✓ {message}")
                elif state == "failed":
                    self._log("err", f"  ✗ 第 {futures[future]:02d} 张：{message}")

                done += 1
                percent = ((index - 1) + done / n) / total * 100
                self._progress(percent, self.stats.saved + results["saved"])
                self._status(f"第 {index}/{total} 篇 · {done}/{n} 张")

                if self.stop.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        return results["saved"], results["skipped"], results["failed"]

    def _one_image(self, url: str, dest: Path, stem: str, referer: str) -> tuple[str, str]:
        if self.stop.is_set():
            raise StopRequested()
        stem_path = dest / stem
        existing = find_existing(stem_path)
        if existing is not None and self.cfg.skip_existing and existing.stat().st_size > 0:
            return "skipped", existing.name
        if existing is not None and not self.cfg.skip_existing:
            try:
                existing.unlink()
            except OSError:
                pass

        data = download_image_bytes(url, self.cfg, self.stop, referer)
        ext = sniff_ext(data) or "jpg"
        final = dest / f"{stem}.{ext}"
        temp = dest / f".{stem}.{ext}.part"
        temp.write_bytes(data)
        os.replace(temp, final)
        return "saved", f"{final.name}  {human_size(len(data))}"


# --------------------------------------------------------------------------
# 配置读写
# --------------------------------------------------------------------------

def default_out_dir() -> Path:
    downloads = Path.home() / "Downloads"
    base = downloads if downloads.is_dir() else Path.home()
    return base / "公众号原图"


def config_file() -> Path:
    base = os.getenv("APPDATA") or os.getenv("XDG_CONFIG_HOME") or str(Path.home())
    return Path(base) / "WeChatImageDownloader" / "config.json"


def load_config() -> dict:
    try:
        return json.loads(config_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict) -> None:
    try:
        path = config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 命令行模式
# --------------------------------------------------------------------------

def run_cli(args: argparse.Namespace) -> int:
    if requests is None:
        print("缺少 requests 库，请先执行：pip install requests")
        return 2

    text = " ".join(args.urls or [])
    if args.file:
        try:
            text += "\n" + decode_html_bytes(Path(args.file).read_bytes())
        except OSError as exc:
            print(f"读取链接文件失败：{exc}")
            return 2

    urls = extract_article_urls(text)
    sources = [Source(url=url) for url in urls]
    for html_path in getattr(args, "html", None) or []:
        try:
            sources.append(Source(
                url=str(html_path),
                html=decode_html_bytes(Path(html_path).read_bytes()),
                label=Path(html_path).name,
            ))
        except OSError as exc:
            print(f"读取网页失败 {html_path}：{exc}")
    if not sources:
        print("没有找到公众号文章链接。")
        return 2

    out_dir = Path(args.out).expanduser() if args.out else default_out_dir()
    cfg = Config(
        out_dir=out_dir,
        workers=max(1, min(8, args.workers)),
        subfolder=not args.no_subdir,
        skip_existing=not args.force,
        save_info=not args.no_info,
        include_gif=not args.no_gif,
        cookie=args.cookie or "",
    )

    lock = threading.Lock()

    def emit(kind: str, payload: object) -> None:
        if kind == "log":
            level, message = payload  # type: ignore
            prefix = {"err": "[错误]", "warn": "[提示]", "ok": "[完成]", "dim": "      "}.get(level, "[信息]")
            with lock:
                print(f"{prefix} {message}", flush=True)

    print(f"{APP_NAME} v{APP_VERSION}")
    print(f"共 {len(sources)} 篇文章，保存到：{out_dir}\n")
    downloader = Downloader(sources, cfg, emit, threading.Event())
    stats = downloader.run()
    print()
    print(
        f"完成：文章 {stats.ok_articles}/{stats.articles} 篇成功，"
        f"图片保存 {stats.saved} 张，跳过 {stats.skipped} 张，失败 {stats.failed} 张。"
    )
    if stats.stopped:
        print("（已中断）")
    return 0 if stats.failed_articles == 0 else 1


# --------------------------------------------------------------------------
# 图形界面
# --------------------------------------------------------------------------

ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAYAAADDPmHLAAACiElEQVR42u3dS1LDMBBFUbmLqrAq2AqsDLYCqyIjGFFFhZ9DEmP3O3fIhETvqtWSwRoDAAAkMi35y3ZPd6+GfB7728dp8wIIfP1CTILPFmESfLYIk+CzRSjhb5tTx7+Eny3BJPjsJaGEn10NSvjZEpThyqbM/uwqUMLPlqCEny2BHkAPYPYnVwEVQAUw+5OrgAqgApj9yVVABVABkMxV6hd/uXn49LPr53sVIGH9/yr8n37euQ8oMz9PglgB5oabJEEJP1sCuwDbQBAABAABQAAQAAQAAUAAEAAEAAFAABAABAABQAAQAAQAAUAAEAAEAAFAABAABAABQAAQAMM7gsZy/8+/hc+wxncQrbYCdHth01q/Txm07O9RBi/782sC7QLMouTPXQYz+/OWQc2WtZTV7EpV1tbsXqVsr7Ib1bLHzj6rcA7gHGA4aQs+qSxn7dnPKsrTtuwHVeWRa/aj6nZN4KXD6fZ3Ci13AZcKqeOtYraBtoE9Ofds7XqnYOsKcK7QOl8o2X4JODW87reJRvQAfw0x4SrZmCbw2DBT7hGO2gXMDTXpEum4beBv4abdIO4cwDlAHt/N8rTZH10BDsNODD9+CXgPPTV8PUB4+AQAAYY3hAxvAwlegq4Evw75/0sES4AewOxPXgZVABUABPjA/vZxcgDTdzdwmK8K4Bzg/613DhB+EGQ5WFkTuFQfgGX5Klc9gAow3xb0mv0qAH4WQBXoPftVAPwugCrQd/bPrgAk6Bn+UUsACfqFrwfAcQKoAr1m/58qAAn6hD/GGCeFuXu6ezXk2wz+LD2AarDt8M/SBJJgu+GfvARYErYb/EUEIMJ2gr+oAERYf/CLCEAIvRUAYLW8AcS17INRRydTAAAAAElFTkSuQmCC"
)

# 配色
C_BG = "#f3f5f8"
C_CARD = "#ffffff"
C_BORDER = "#e2e6ee"
C_TEXT = "#1c2128"
C_MUTED = "#7d8797"
C_ACCENT = "#07c160"
C_ACCENT_HOVER = "#06ad56"
C_ACCENT_PRESS = "#059b4c"
C_GHOST_HOVER = "#f1f4f8"
C_DANGER = "#e5484d"
C_WARN = "#d97706"
C_OK = "#0a9d5a"


def _rounded_points(x1: float, y1: float, x2: float, y2: float, r: float) -> list[float]:
    return [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]


class RoundButton:
    """用 Canvas 画的圆角按钮，外观比原生 ttk.Button 干净。"""

    def __init__(self, master, text: str, command, x: int = 0, y: int = 0,
                 width: int = 110, height: int = 34, radius: int = 9,
                 fill: str = C_ACCENT, fg: str = "#ffffff",
                 hover: str = C_ACCENT_HOVER, press: str = C_ACCENT_PRESS,
                 border: str = "", bg: str = C_CARD,
                 font=("Microsoft YaHei UI", 10), bold: bool = False):
        import tkinter as tk

        self.canvas = tk.Canvas(master, width=width, height=height, bg=bg,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.fill, self.fg = fill, fg
        self.hover, self.press = hover, press
        self.border = border
        self.command = command
        self.enabled = True
        self._pressed = False
        self._hovering = False
        self._text = text
        self._w, self._h, self._r = width, height, radius
        self._font = (font[0], font[1], "bold") if bold else font

        self._shape = self.canvas.create_polygon(
            _rounded_points(1, 1, width - 1, height - 1, radius),
            smooth=True, splinesteps=36, fill=fill,
            outline=border if border else fill, width=1,
        )
        self._label = self.canvas.create_text(
            width / 2, height / 2 + 1, text=text, fill=fg, font=self._font
        )
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    # 兼容 pack/grid 的常见写法
    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)
        return self

    def grid(self, **kwargs):
        self.canvas.grid(**kwargs)
        return self

    def place(self, **kwargs):
        self.canvas.place(**kwargs)
        return self

    def configure_text(self, text: str) -> None:
        self._text = text
        self.canvas.itemconfigure(self._label, text=text)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.canvas.configure(cursor="hand2" if enabled else "arrow")
        self._refresh()

    def _refresh(self) -> None:
        if not self.enabled:
            fill = "#e9edf2" if not self.border else "#ffffff"
            fg = "#a7b0bd" if not self.border else "#b8c0cb"
            border = fill if not self.border else C_BORDER
        else:
            if self._pressed:
                fill = self.press
            elif self._hovering:
                fill = self.hover
            else:
                fill = self.fill
            fg = self.fg
            border = self.border if self.border else fill
        self.canvas.itemconfigure(self._shape, fill=fill, outline=border)
        self.canvas.itemconfigure(self._label, fill=fg)

    def _on_enter(self, _event) -> None:
        if self.enabled:
            self._hovering = True
            self._refresh()

    def _on_leave(self, _event) -> None:
        self._hovering = False
        self._pressed = False
        self._refresh()

    def _on_press(self, _event) -> None:
        if self.enabled:
            self._pressed = True
            self._refresh()

    def _on_release(self, _event) -> None:
        if not self.enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._refresh()
        if was_pressed and self._hovering and callable(self.command):
            self.command()


class Logo:
    """用 Canvas 现画的下载图标，避免额外图片依赖。"""

    def __init__(self, master, size: int = 46, bg: str = C_CARD):
        import tkinter as tk

        self.canvas = tk.Canvas(master, width=size, height=size, bg=bg,
                                highlightthickness=0, bd=0)
        s = size
        self.canvas.create_polygon(
            _rounded_points(0, 0, s, s, s * 0.28),
            smooth=True, splinesteps=40, fill=C_ACCENT, outline=C_ACCENT,
        )
        cx = s / 2
        self.canvas.create_line(cx, s * 0.22, cx, s * 0.60,
                                fill="white", width=max(3, int(s * 0.09)),
                                capstyle="round")
        self.canvas.create_polygon(
            s * 0.30, s * 0.50, cx, s * 0.74, s * 0.70, s * 0.50,
            fill="white", outline="white",
        )
        self.canvas.create_line(
            s * 0.24, s * 0.80, s * 0.76, s * 0.80,
            fill="white", width=max(3, int(s * 0.08)), capstyle="round",
        )


HELP_TEXT = """怎么用

1. 在浏览器或微信里复制文章链接（短链 https://mp.weixin.qq.com/s/xxxx 或带 __biz 的长链都行）。
2. 每行粘贴一个，可以一次放很多篇，然后点「开始下载」。
3. 图片默认保存到「下载 / 公众号原图」，每篇文章一个「日期_标题」文件夹。

为什么叫“原图”

微信正文里显示的图片，地址末尾通常是 /640，那是压缩后的版本（一般 1080px 宽）。
本工具会把它替换成 /0，取回作者上传时的原始分辨率，所以文件更大、更清晰。
可在「文章信息.txt」里对照原文链接核对。

遇到“环境异常 / 需要验证”

微信对部分文章会要求验证，接口取不到正文。此时：
1. 用浏览器打开该文章，等图片加载完；
2. Ctrl+S 把网页另存为 .html；
3. 回到本软件点「本地 HTML」，选择刚保存的文件即可。

其他说明

- 支持 jpg / png / gif / webp，GIF 会保留动画。
- 重复下载会自动跳过已有图片，可断点续传。
- 图片版权归原作者所有，请仅用于个人备份、学习，不要二次传播。"""


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.root.title(f"{APP_NAME}  v{APP_VERSION}")
        self.root.configure(bg=C_BG)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        win_w = min(1060, max(900, screen_w - 80))
        win_h = min(780, max(640, screen_h - 100))
        self.root.geometry(f"{win_w}x{win_h}")
        self.root.minsize(min(940, win_w), min(680, win_h))

        self._center_window(win_w, win_h)
        self._setup_style()

        self.events: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self._closed = False
        self._placeholder_on = False
        self._last_out_dir = default_out_dir()

        cfg = load_config()
        self.out_var = tk.StringVar(value=cfg.get("out_dir") or str(default_out_dir()))
        self.subfolder_var = tk.BooleanVar(value=bool(cfg.get("subfolder", True)))
        self.skip_var = tk.BooleanVar(value=bool(cfg.get("skip_existing", True)))
        self.info_var = tk.BooleanVar(value=bool(cfg.get("save_info", True)))
        self.gif_var = tk.BooleanVar(value=bool(cfg.get("include_gif", True)))
        self.workers_var = tk.IntVar(value=int(cfg.get("workers", 4) or 4))

        self._load_window_icon()
        self._build_ui()
        self._bind_shortcuts()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._poll_events)
        self.url_text.focus_set()

    # -- 初始化辅助 ---------------------------------------------------------
    def _center_window(self, w: int, h: int) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _setup_style(self) -> None:
        style = self.ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        font = ("Microsoft YaHei UI", 10)
        style.configure(".", font=font, background=C_CARD, foreground=C_TEXT)
        style.configure("Card.TCheckbutton", background=C_CARD, foreground=C_TEXT,
                        font=font, focuscolor=C_CARD)
        style.map(
            "Card.TCheckbutton",
            background=[("active", C_CARD), ("pressed", C_CARD)],
            indicatorbackground=[("selected", C_ACCENT), ("!selected", "#ffffff")],
            indicatorforeground=[("selected", "#ffffff")],
        )
        style.configure("Card.TEntry", fieldbackground="#fbfcfe", foreground=C_TEXT,
                        bordercolor=C_BORDER, lightcolor=C_BORDER, darkcolor=C_BORDER,
                        insertcolor=C_TEXT, padding=6)
        style.configure("Card.TSpinbox", fieldbackground="#fbfcfe", foreground=C_TEXT,
                        bordercolor=C_BORDER, lightcolor=C_BORDER, darkcolor=C_BORDER,
                        arrowcolor="#5b6472", padding=5)
        style.configure("Green.Horizontal.TProgressbar", troughcolor="#edf0f5",
                        background=C_ACCENT, bordercolor="#edf0f5",
                        lightcolor=C_ACCENT, darkcolor=C_ACCENT, thickness=8)
        style.configure("Vertical.TScrollbar", gripcount=0, background="#dbe1e9",
                        darkcolor="#dbe1e9", lightcolor="#dbe1e9",
                        troughcolor="#f8fafc", bordercolor="#f8fafc",
                        arrowcolor="#8b95a4", arrowsize=9, width=10,
                        relief="flat", borderwidth=0)
        style.map("Vertical.TScrollbar",
                  background=[("active", "#c6ceda"), ("pressed", "#b6c0cd")])

    def _load_window_icon(self) -> None:
        try:
            data = base64.b64decode(ICON_B64)
            self._icon = self.tk.PhotoImage(data=data)
            self.root.iconphoto(True, self._icon)
        except Exception:
            self._icon = None

    def _card(self, parent) -> tuple:
        card = self.tk.Frame(parent, bg=C_CARD, highlightthickness=1,
                             highlightbackground=C_BORDER, highlightcolor=C_BORDER)
        inner = self.tk.Frame(card, bg=C_CARD)
        inner.pack(fill="both", expand=True, padx=16, pady=14)
        return card, inner

    def _section_title(self, parent, text: str, hint: str = ""):
        row = self.tk.Frame(parent, bg=C_CARD)
        row.pack(fill="x", pady=(0, 8))
        self.tk.Label(row, text=text, bg=C_CARD, fg=C_TEXT,
                      font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        hint_label = None
        if hint:
            hint_label = self.tk.Label(row, text=hint, bg=C_CARD, fg=C_MUTED,
                                       font=("Microsoft YaHei UI", 9))
            hint_label.pack(side="right")
        return hint_label

    # -- 界面 ---------------------------------------------------------------
    def _build_ui(self) -> None:
        tk = self.tk
        header = tk.Frame(self.root, bg=C_CARD, height=86)
        header.pack(fill="x")
        header.pack_propagate(False)

        left = tk.Frame(header, bg=C_CARD)
        left.pack(side="left", padx=22, pady=16)
        Logo(left, 48).canvas.pack(side="left")
        titles = tk.Frame(left, bg=C_CARD)
        titles.pack(side="left", padx=(12, 0))
        tk.Label(titles, text=APP_NAME, bg=C_CARD, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        tk.Label(titles, text="批量提取正文配图 · 自动替换原图 · 无需登录",
                 bg=C_CARD, fg=C_MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(3, 0))

        right = tk.Frame(header, bg=C_CARD)
        right.pack(side="right", padx=22)
        RoundButton(right, "使用说明", self._show_help, width=88, height=30,
                    fill="#f5f7fa", fg="#4a5462", hover="#eceff4", press="#e2e6ec",
                    border=C_BORDER, font=("Microsoft YaHei UI", 9)).pack(side="right")
        tk.Label(right, text=f"v{APP_VERSION}", bg=C_CARD, fg="#b3bcc9",
                 font=("Segoe UI", 9)).pack(side="right", padx=(0, 12))

        tk.Frame(self.root, bg=C_BORDER, height=1).pack(fill="x")

        body = tk.Frame(self.root, bg=C_BG)
        body.pack(fill="both", expand=True, padx=18, pady=(14, 16))
        body.grid_columnconfigure(0, weight=3, uniform="col")
        body.grid_columnconfigure(1, weight=2, uniform="col")
        body.grid_rowconfigure(2, weight=1)

        self._build_link_card(body).grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self._build_settings_card(body).grid(row=0, column=1, sticky="nsew")
        self._build_action_card(body).grid(row=1, column=0, columnspan=2,
                                           sticky="ew", pady=(12, 0))
        self._build_log_card(body).grid(row=2, column=0, columnspan=2,
                                        sticky="nsew", pady=(12, 0))

    def _build_link_card(self, parent):
        tk = self.tk
        card, inner = self._card(parent)
        self.link_hint = self._section_title(inner, "文章链接", "每行一个，可一次粘贴多篇")

        text_wrap = tk.Frame(inner, bg=C_CARD)
        text_wrap.pack(fill="both", expand=True)
        self.url_text = tk.Text(
            text_wrap, height=9, wrap="word", relief="flat", bd=0,
            bg="#fbfcfe", fg=C_TEXT, insertbackground=C_TEXT,
            font=("Microsoft YaHei UI", 10), padx=10, pady=8,
            highlightthickness=1, highlightbackground=C_BORDER,
            highlightcolor=C_BORDER, undo=True,
        )
        scroll = self.ttk.Scrollbar(text_wrap, orient="vertical",
                                    command=self.url_text.yview)
        self.url_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.url_text.pack(side="left", fill="both", expand=True)
        self._set_placeholder()
        self.url_text.bind("<KeyPress>", self._maybe_clear_placeholder)
        self.url_text.bind("<<Paste>>", self._on_paste_event)
        self.url_text.bind("<KeyRelease>", lambda _e: self._update_link_count())
        self.url_text.bind("<FocusOut>", self._restore_placeholder_event)

        buttons = tk.Frame(inner, bg=C_CARD)
        buttons.pack(fill="x", pady=(10, 0))
        ghost = dict(fill="#ffffff", fg="#3c4553", hover=C_GHOST_HOVER,
                     press="#e9edf2", border=C_BORDER, font=("Microsoft YaHei UI", 9))
        RoundButton(buttons, "粘贴", self._paste_clipboard, width=66, height=30, **ghost).pack(side="left")
        RoundButton(buttons, "导入 TXT", self._import_txt, width=82, height=30,
                    **ghost).pack(side="left", padx=(8, 0))
        RoundButton(buttons, "本地 HTML", self._import_html, width=88, height=30,
                    **ghost).pack(side="left", padx=(8, 0))
        RoundButton(buttons, "清空", self._clear_urls, width=58, height=30,
                    **ghost).pack(side="right")
        return card

    def _build_settings_card(self, parent):
        tk = self.tk
        card, inner = self._card(parent)
        self._section_title(inner, "下载设置")

        tk.Label(inner, text="保存位置", bg=C_CARD, fg=C_MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        row = tk.Frame(inner, bg=C_CARD)
        row.pack(fill="x", pady=(4, 12))
        self.out_entry = self.ttk.Entry(row, textvariable=self.out_var,
                                        style="Card.TEntry", font=("Microsoft YaHei UI", 9))
        self.out_entry.pack(side="left", fill="x", expand=True)
        RoundButton(row, "选择…", self._choose_dir, width=62, height=30,
                    fill="#ffffff", fg="#3c4553", hover=C_GHOST_HOVER, press="#e9edf2",
                    border=C_BORDER, font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(8, 0))

        for text, var in (
            ("每篇文章单独建文件夹", self.subfolder_var),
            ("跳过已下载的图片（可续传）", self.skip_var),
            ("保存 文章信息.txt", self.info_var),
            ("包含 GIF 动图", self.gif_var),
        ):
            self.ttk.Checkbutton(inner, text=text, variable=var,
                                 style="Card.TCheckbutton").pack(anchor="w", pady=2)

        workers = tk.Frame(inner, bg=C_CARD)
        workers.pack(fill="x", pady=(12, 0))
        tk.Label(workers, text="同时下载", bg=C_CARD, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.ttk.Spinbox(workers, from_=1, to=8, width=4, textvariable=self.workers_var,
                         style="Card.TSpinbox", justify="center").pack(side="left", padx=8)
        tk.Label(workers, text="张（过高可能被微信限流）", bg=C_CARD, fg=C_MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        return card

    def _build_action_card(self, parent):
        tk = self.tk
        card, inner = self._card(parent)
        top = tk.Frame(inner, bg=C_CARD)
        top.pack(fill="x")

        self.start_btn = RoundButton(top, "开始下载", self._start, width=128, height=40,
                                     radius=10, font=("Microsoft YaHei UI", 10), bold=True)
        self.start_btn.pack(side="left")
        self.stop_btn = RoundButton(top, "停止", self._stop, width=82, height=40,
                                    fill="#ffffff", fg="#4a5462", hover="#f4f6f9",
                                    press="#e9edf2", border=C_BORDER,
                                    font=("Microsoft YaHei UI", 10))
        self.stop_btn.pack(side="left", padx=(10, 0))
        self.stop_btn.set_enabled(False)

        self.percent_label = tk.Label(top, text="0%", bg=C_CARD, fg=C_MUTED,
                                      font=("Segoe UI", 11, "bold"))
        self.percent_label.pack(side="right")
        self.status_label = tk.Label(top, text="准备就绪", bg=C_CARD, fg=C_MUTED,
                                     font=("Microsoft YaHei UI", 9), anchor="w")
        self.status_label.pack(side="left", padx=(16, 0))

        self.progress = self.ttk.Progressbar(inner, style="Green.Horizontal.TProgressbar",
                                             mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(12, 0))
        return card

    def _build_log_card(self, parent):
        tk = self.tk
        card, inner = self._card(parent)
        row = tk.Frame(inner, bg=C_CARD)
        row.pack(fill="x", pady=(0, 8))
        tk.Label(row, text="运行日志", bg=C_CARD, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        RoundButton(row, "打开文件夹", self._open_folder, width=92, height=28,
                    fill="#ffffff", fg="#3c4553", hover=C_GHOST_HOVER, press="#e9edf2",
                    border=C_BORDER, font=("Microsoft YaHei UI", 9)).pack(side="right")

        wrap = tk.Frame(inner, bg=C_CARD)
        wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            wrap, height=8, wrap="word", relief="flat", bd=0,
            bg="#fbfcfe", fg="#4a5462", insertbackground=C_TEXT,
            font=("Microsoft YaHei UI", 9), padx=10, pady=8,
            highlightthickness=1, highlightbackground=C_BORDER,
            highlightcolor=C_BORDER, state="disabled",
        )
        scroll = self.ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("time", foreground="#aab2bf")
        self.log_text.tag_configure("info", foreground="#4a5462")
        self.log_text.tag_configure("dim", foreground="#8d96a5")
        self.log_text.tag_configure("ok", foreground=C_OK)
        self.log_text.tag_configure("warn", foreground=C_WARN)
        self.log_text.tag_configure("err", foreground=C_DANGER)
        self._append_log("info", "把文章链接粘贴到左边，点「开始下载」即可。")
        self._append_log("dim", "图片会自动取回原始分辨率；遇到需要验证的文章，可用「本地 HTML」。")
        return card

    def _bind_shortcuts(self) -> None:
        self.url_text.bind("<Control-Return>", self._shortcut_start)
        self.root.bind("<F1>", lambda _e: self._show_help())

    def _shortcut_start(self, _event):
        self._start()
        return "break"

    # -- 输入框占位符 -------------------------------------------------------
    PLACEHOLDER = (
        "在此粘贴公众号文章链接，例如：\n"
        "https://mp.weixin.qq.com/s/xxxxxxxxxxxx\n\n"
        "支持短链接，也支持带 __biz 参数的长链接；每行一个，可批量粘贴。"
    )

    def _set_placeholder(self) -> None:
        self.url_text.delete("1.0", "end")
        self.url_text.insert("1.0", self.PLACEHOLDER)
        self.url_text.configure(fg="#a8b0bc")
        self._placeholder_on = True

    def _clear_placeholder(self) -> None:
        if self._placeholder_on:
            self.url_text.delete("1.0", "end")
            self.url_text.configure(fg=C_TEXT)
            self._placeholder_on = False

    def _clear_placeholder_event(self, _event=None) -> None:
        if self._placeholder_on:
            self._clear_placeholder()

    def _maybe_clear_placeholder(self, event) -> None:
        # 有真实输入（含退格）时才清掉灰色提示；纯 Ctrl 组合键不清
        if not self._placeholder_on:
            return
        if event.state & 0x0004:  # Control
            return
        if event.char or event.keysym in ("BackSpace", "Delete"):
            self._clear_placeholder()
            self.url_text.see("end")

    def _on_paste_event(self, _event=None) -> None:
        # Ctrl+V 粘贴前先移除灰色提示，避免粘到提示文字后面
        self._clear_placeholder()

    def _restore_placeholder_event(self, _event=None) -> None:
        if not self.url_text.get("1.0", "end").strip():
            self._set_placeholder()
        self._update_link_count()

    def _update_link_count(self) -> None:
        if not hasattr(self, "link_hint") or self.link_hint is None:
            return
        if self._placeholder_on:
            urls = []
        else:
            urls = extract_article_urls(self.url_text.get("1.0", "end"))
        if urls:
            self.link_hint.configure(text=f"已识别 {len(urls)} 个链接", fg=C_ACCENT)
        else:
            self.link_hint.configure(text="每行一个，可一次粘贴多篇", fg=C_MUTED)

    # -- 按钮行为 -----------------------------------------------------------
    def _paste_clipboard(self) -> None:
        try:
            content = self.root.clipboard_get()
        except Exception:
            self._append_log("warn", "剪贴板里没有文本。")
            return
        self._clear_placeholder()
        current = self.url_text.get("1.0", "end").strip()
        if current:
            self.url_text.insert("end", "\n")
        self.url_text.insert("end", content.strip())
        self.url_text.see("end")
        self._update_link_count()

    def _import_txt(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title="选择包含链接的文本文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            content = decode_html_bytes(Path(path).read_bytes())
        except OSError as exc:
            self._append_log("err", f"读取失败：{exc}")
            return
        self._clear_placeholder()
        self.url_text.insert("end", content.strip())
        self.url_text.see("end")
        self._update_link_count()
        self._append_log("info", f"已导入：{Path(path).name}")

    def _import_html(self) -> None:
        from tkinter import filedialog

        if self.worker is not None and self.worker.is_alive():
            self._append_log("warn", "正在下载中，请先停止再操作。")
            return
        paths = filedialog.askopenfilenames(
            title="选择已保存的文章网页（可多选）",
            filetypes=[("网页文件", "*.html;*.htm"), ("所有文件", "*.*")],
        )
        if not paths:
            return
        sources = []
        for path in paths:
            try:
                text = decode_html_bytes(Path(path).read_bytes())
            except OSError as exc:
                self._append_log("err", f"读取失败 {Path(path).name}：{exc}")
                continue
            sources.append(Source(url=str(path), html=text, label=Path(path).name))
        if sources:
            self._append_log("info", f"从本地网页导入 {len(sources)} 个文件。")
            self._begin(sources)

    def _clear_urls(self) -> None:
        self.url_text.delete("1.0", "end")
        self._placeholder_on = False
        self._set_placeholder()
        self._update_link_count()
        self.url_text.focus_set()

    def _choose_dir(self) -> None:
        from tkinter import filedialog

        initial = self.out_var.get().strip() or str(default_out_dir())
        path = filedialog.askdirectory(title="选择保存位置", initialdir=initial)
        if path:
            self.out_var.set(path)

    def _open_folder(self) -> None:
        path = Path(self.out_var.get().strip() or str(default_out_dir()))
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except Exception as exc:
            self._append_log("err", f"打开文件夹失败：{exc}")

    # -- 下载流程 -----------------------------------------------------------
    def _current_cfg(self) -> Config:
        out_dir = Path(self.out_var.get().strip() or str(default_out_dir())).expanduser()
        try:
            workers = int(self.workers_var.get())
        except Exception:
            workers = 4
        return Config(
            out_dir=out_dir,
            workers=max(1, min(8, workers)),
            subfolder=bool(self.subfolder_var.get()),
            skip_existing=bool(self.skip_var.get()),
            save_info=bool(self.info_var.get()),
            include_gif=bool(self.gif_var.get()),
        )

    def _start(self) -> None:
        from tkinter import messagebox

        if self.worker is not None and self.worker.is_alive():
            return
        text = "" if self._placeholder_on else self.url_text.get("1.0", "end")
        urls = extract_article_urls(text)
        if not urls:
            messagebox.showwarning("没有找到链接",
                                   "请先粘贴公众号文章链接，例如：\nhttps://mp.weixin.qq.com/s/xxxxxxxx")
            return
        self._begin([Source(url=u) for u in urls])

    def _begin(self, sources: list[Source]) -> None:
        from tkinter import messagebox

        cfg = self._current_cfg()
        try:
            cfg.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("保存位置不可用", f"无法创建目录：\n{cfg.out_dir}\n\n{exc}")
            return

        self._last_out_dir = cfg.out_dir
        self._save_settings(cfg)
        self.stop_event.clear()
        self.progress.configure(value=0)
        self.percent_label.configure(text="0%")
        self.status_label.configure(text=f"准备下载 {len(sources)} 篇文章…", fg=C_MUTED)
        self.start_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self._append_log("info", f"———— 开始下载 {len(sources)} 篇文章 ————")
        self._append_log("dim", f"保存到：{cfg.out_dir}")

        self.worker = threading.Thread(target=self._work, args=(sources, cfg), daemon=True)
        self.worker.start()

    def _work(self, sources: list[Source], cfg: Config) -> None:
        try:
            downloader = Downloader(sources, cfg, self._emit, self.stop_event)
            stats = downloader.run()
            self.events.put(("done", stats))
        except Exception as exc:  # 防御性兜底
            self.events.put(("fatal", f"{exc.__class__.__name__}: {exc}"))

    def _emit(self, kind: str, payload: object) -> None:
        self.events.put((kind, payload))

    def _stop(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            self.stop_event.set()
            self.status_label.configure(text="正在停止，等待当前请求结束…", fg=C_WARN)
            self._append_log("warn", "已请求停止。")
            self.stop_btn.set_enabled(False)

    # -- 事件循环 -----------------------------------------------------------
    def _poll_events(self) -> None:
        if self._closed:
            return
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    level, message = payload  # type: ignore
                    self._append_log(level, message)
                elif kind == "status":
                    text = str(payload or "")
                    if text:
                        self.status_label.configure(text=text, fg=C_MUTED)
                elif kind == "progress":
                    percent, saved = payload  # type: ignore
                    self.progress.configure(value=percent)
                    if saved is None:
                        self.percent_label.configure(text=f"{percent:.0f}%")
                    else:
                        self.percent_label.configure(text=f"{percent:.0f}% · {saved} 张")
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "fatal":
                    self._on_fatal(str(payload))
        except queue.Empty:
            pass
        except Exception as exc:
            print("事件处理异常：", exc)
        self.root.after(80, self._poll_events)

    def _on_done(self, stats: Stats) -> None:
        self.start_btn.set_enabled(True)
        self.stop_btn.set_enabled(False)
        if stats.stopped:
            self.status_label.configure(
                text=f"已停止：保存 {stats.saved} 张，跳过 {stats.skipped} 张", fg=C_WARN)
            self._append_log("warn",
                             f"下载已停止。已保存 {stats.saved} 张，跳过 {stats.skipped} 张。")
        else:
            self.progress.configure(value=100)
            self.percent_label.configure(text="100%")
            self.status_label.configure(
                text=(f"完成：{stats.ok_articles}/{stats.articles} 篇 · "
                      f"保存 {stats.saved} 张 · 跳过 {stats.skipped} 张 · 失败 {stats.failed} 张"),
                fg=C_OK if stats.failed == 0 else C_WARN,
            )
            self._append_log(
                "ok",
                f"全部完成：文章 {stats.ok_articles}/{stats.articles} 篇，"
                f"图片 {stats.saved} 张，跳过 {stats.skipped} 张，失败 {stats.failed} 张。",
            )
        self.worker = None

    def _on_fatal(self, message: str) -> None:
        from tkinter import messagebox

        self.start_btn.set_enabled(True)
        self.stop_btn.set_enabled(False)
        self.worker = None
        self.status_label.configure(text="任务异常终止", fg=C_DANGER)
        self._append_log("err", message)
        messagebox.showerror("出错了", message)

    def _append_log(self, level: str, message: str) -> None:
        if not hasattr(self, "log_text"):
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{timestamp}  ", "time")
        self.log_text.insert("end", message + "\n", level if level != "info" else "info")
        # 限制日志长度，避免长时间运行越来越卡
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 6000:
            self.log_text.delete("1.0", "1200.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- 其他 ---------------------------------------------------------------
    def _save_settings(self, cfg: Config) -> None:
        save_config({
            "out_dir": str(cfg.out_dir),
            "subfolder": cfg.subfolder,
            "skip_existing": cfg.skip_existing,
            "save_info": cfg.save_info,
            "include_gif": cfg.include_gif,
            "workers": cfg.workers,
        })

    def _show_help(self) -> None:
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title("使用说明")
        win.configure(bg=C_CARD)
        win.transient(self.root)
        win.resizable(False, False)
        width, height = 660, 620
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")

        tk.Label(win, text="使用说明", bg=C_CARD, fg=C_TEXT,
                 font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w", padx=22, pady=(18, 8))

        # 先放底部按钮，保证小窗口下按钮不会被正文挤出去
        RoundButton(win, "知道了", win.destroy, width=104, height=36,
                    radius=9, bg=C_CARD).pack(side="bottom", pady=(10, 18))

        body = tk.Frame(win, bg=C_CARD)
        body.pack(fill="both", expand=True, padx=(22, 12))
        text = tk.Text(body, wrap="word", relief="flat", bd=0, bg=C_CARD, fg="#3f4855",
                       font=("Microsoft YaHei UI", 9), padx=0, pady=2,
                       highlightthickness=0, spacing1=2, spacing3=5, cursor="arrow")
        scroll = self.ttk.Scrollbar(body, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")
        text.bind("<MouseWheel>", lambda e: text.yview_scroll(int(-e.delta / 120), "units"))
        text.bind("<Button-4>", lambda e: text.yview_scroll(-3, "units"))
        text.bind("<Button-5>", lambda e: text.yview_scroll(3, "units"))
        win.grab_set()
        win.focus_set()

    def _on_close(self) -> None:
        from tkinter import messagebox

        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno("退出", "正在下载，确定要停止并退出吗？"):
                return
            self.stop_event.set()
        self._closed = True
        self._save_settings(self._current_cfg())
        self.root.destroy()


def launch_gui() -> int:
    if requests is None:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "缺少依赖",
                "缺少 requests 库。\n\n请先执行：pip install requests\n"
                "或使用打包好的 exe 版本。",
            )
            root.destroy()
        except Exception:
            print("缺少 requests 库，请先执行：pip install requests")
        return 2

    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:
        pass

    import tkinter as tk

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    # 打包成 --windowed 的 exe 后没有控制台，sys.stdout/stderr 为 None，
    # 直接 print 会报错；这里兜底到空设备，保证命令行模式也能静默工作。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="wx_img_downloader",
        description="批量下载微信公众号文章正文图片原图",
    )
    parser.add_argument("urls", nargs="*", help="文章链接，可多个")
    parser.add_argument("-o", "--out", help="保存目录")
    parser.add_argument("-f", "--file", help="包含链接的文本文件")
    parser.add_argument("--html", action="append", metavar="HTML",
                        help="解析本地保存的文章网页（可重复）")
    parser.add_argument("-j", "--workers", type=int, default=4, help="图片并发数（1-8）")
    parser.add_argument("--cli", action="store_true", help="命令行模式")
    parser.add_argument("--no-subdir", action="store_true", help="不为每篇文章建子文件夹")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的图片")
    parser.add_argument("--no-info", action="store_true", help="不保存文章信息.txt")
    parser.add_argument("--no-gif", action="store_true", help="跳过 GIF 动图")
    parser.add_argument("--cookie", help="可选：自定义 Cookie")
    args = parser.parse_args(argv)

    cli_mode = args.cli or bool(args.urls) or bool(args.file) or bool(args.html)
    if cli_mode:
        return run_cli(args)
    return launch_gui()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
