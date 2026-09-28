import asyncio
import io
import os
import re
import secrets
import time
from collections import defaultdict
from html.parser import HTMLParser
from typing import DefaultDict, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import primp
import uvicorn
import trafilatura
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from mcp.types import ImageContent
from PIL import Image as PILImage
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# EXPECTED_TOKEN = os.getenv("AUTH_TOKEN", "")
# if not EXPECTED_TOKEN:
#     raise RuntimeError("AUTH_TOKEN environment variable must be set.")

# --- Rate limit: не более RATE_LIMIT_MAX запросов с одного IP за RATE_LIMIT_WINDOW секунд ---
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))

PORT = int(os.getenv("PORT", "8000"))

# --- Поиск: сколько результатов просить у DuckDuckGo ---
DEFAULT_MAX_RESULTS = int(os.getenv("DEFAULT_MAX_RESULTS", "3"))
MAX_RESULTS_HARD_LIMIT = 15

# --- Изображения: сколько скачивать и как обрабатывать ---
DEFAULT_MAX_IMAGES = int(os.getenv("DEFAULT_MAX_IMAGES", "3"))
MAX_IMAGES_HARD_LIMIT = 10
SEARCH_IMAGES_HARD_LIMIT = 30
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))  # потолок размера файла
MAX_IMAGE_DIM = int(os.getenv("MAX_IMAGE_DIM", "1024"))  # большие картинки ужимаются до этой стороны
MIN_IMAGE_DIM = 150  # всё мельче считается иконкой/мусором
IMAGE_TIMEOUT = float(os.getenv("IMAGE_TIMEOUT", "15"))

# Клиент с имитацией TLS-фингерпринта Chrome: без него Wikimedia и др. отдают 403
_IMG_CLIENT = primp.Client(impersonate="chrome", timeout=IMAGE_TIMEOUT, follow_redirects=True)

# URL картинок с такими словами — служебные (иконки, трекеры, спрайты), пропускаем
_SKIP_IMG_RE = re.compile(
    r"pixel|spacer|blank|beacon|tracking|sprite|emoji|avatar|icon|logo|badge|loading",
    re.IGNORECASE,
)


mcp = FastMCP("websearch")


def _search_duckduckgo_sync(q: str, max_results: int) -> List[Dict[str, str]]:
    """Синхронный поиск в DuckDuckGo; возвращает список словарей {title, link, snippet}."""
    raw = DDGS().text(q, max_results=max_results)
    results: List[Dict[str, str]] = []
    for item in raw:
        link = item.get("href", "").strip()
        title = item.get("title", "").strip()
        if not link or not title:
            continue
        results.append(
            {
                "title": title,
                "link": link,
                "snippet": item.get("body", "").strip(),
            }
        )
    return results


async def _websearch(q: str, max_results: int):
    """Форматирует результаты поиска в текст; вызов DDG уносится в поток, чтобы не блокировать event loop."""
    max_results = max(1, min(max_results, MAX_RESULTS_HARD_LIMIT))
    try:
        results = await asyncio.to_thread(_search_duckduckgo_sync, q, max_results)
    except DDGSException as exc:
        return f"Search failed: {exc}"
    except Exception as exc:
        return f"Search failed: {exc}"
    if not results:
        return "No results found."
    lines: List[str] = []
    for i, r in enumerate(results, 1):
        block = f"{i}. {r['title']}\n   {r['link']}"
        if r.get("snippet"):
            block += f"\n   {r['snippet']}"
        lines.append(block)
    return "\n".join(lines)

@mcp.tool(
    title="websearch (DuckDuckGo)",
    description="Performs a web search using DuckDuckGo with parameter q (query - the search term) "
                "and max_results (integer)"
)
async def websearch(q: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """MCP-инструмент: поиск в интернете через DuckDuckGo."""
    return await _websearch(q, max_results)


def _parse_dim(value: Optional[str]) -> Optional[int]:
    """Вытаскивает число из атрибута width/height (там бывает '250px' и т.п.)."""
    if not value:
        return None
    match = re.match(r"\d+", value.strip())
    return int(match.group()) if match else None


class _ImgTagParser(HTMLParser):
    """Собирает из HTML теги <img>: src (включая lazy-load data-src), alt и размеры."""

    def __init__(self) -> None:
        super().__init__()
        self.images: List[Dict[str, object]] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "img":
            return
        attr = {k.lower(): v for k, v in attrs if v}
        src = attr.get("src") or attr.get("data-src") or attr.get("data-lazy-src")
        if not src or src.startswith(("data:", "javascript:")):
            return
        self.images.append(
            {
                "src": src,
                "alt": (attr.get("alt") or "").strip(),
                "width": _parse_dim(attr.get("width")),
                "height": _parse_dim(attr.get("height")),
            }
        )


def _extract_image_candidates(html: str, base_url: str, limit: int) -> List[Dict[str, object]]:
    """Отбирает из HTML до limit картинок-кандидатов на скачивание.

    Отсекает служебные изображения (иконки, трекеры — по _SKIP_IMG_RE),
    мелкие по атрибутам width/height и дубликаты; относительные URL
    приводит к абсолютным относительно base_url.
    """
    parser = _ImgTagParser()
    try:
        parser.feed(html)
    except Exception:
        pass
    seen = set()
    candidates: List[Dict[str, object]] = []
    for img in parser.images:
        url = urljoin(base_url, str(img["src"]))
        if urlparse(url).scheme not in ("http", "https"):
            continue
        if _SKIP_IMG_RE.search(url):
            continue
        width, height = img["width"], img["height"]
        if isinstance(width, int) and isinstance(height, int) and 0 < min(width, height) < 100:
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append({"url": url, "alt": img["alt"]})
        if len(candidates) >= limit:
            break
    return candidates


def _fetch_and_process_image(url: str) -> Optional[Tuple[bytes, str, Tuple[int, int]]]:
    """Скачивает картинку и готовит её к отдаче в MCP; возвращает (байты, формат, размер) или None.

    Отбрасывает SVG, файлы больше MAX_IMAGE_BYTES и изображения мельче MIN_IMAGE_DIM;
    крупные ужимает до MAX_IMAGE_DIM. С альфа-каналом сохраняет как PNG, иначе как JPEG.
    """
    try:
        resp = _IMG_CLIENT.get(url)
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type.startswith("image/") or "svg" in content_type:
            return None
        buffer = resp.content
        if not buffer or len(buffer) > MAX_IMAGE_BYTES:
            return None
        with PILImage.open(io.BytesIO(buffer)) as img:
            img.load()
            if min(img.size) < MIN_IMAGE_DIM:
                return None
            if max(img.size) > MAX_IMAGE_DIM:
                img.thumbnail((MAX_IMAGE_DIM, MAX_IMAGE_DIM))
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            out = io.BytesIO()
            if has_alpha:
                img.save(out, format="PNG", optimize=True)
                return out.getvalue(), "png", img.size
            img.convert("RGB").save(out, format="JPEG", quality=85)
            return out.getvalue(), "jpeg", img.size
    except Exception:
        return None


async def _get_page_content(url: str, max_images: int) -> str | list[str | ImageContent]:
    """Извлекает чистый текст страницы (trafilatura) и до max_images важных картинок с неё.

    Возвращает строку (только текст), если картинок нет или max_images=0,
    либо список [текст со списком картинок, ImageContent, ...] — MCP image-блоки.
    Картинки скачиваются конкурентно; берутся первые max_images успешно загруженных.
    """
    scheme = urlparse(url).scheme
    if scheme not in ("http", "https"):
        return f"Unsupported URL scheme: {scheme or '(none)'}. Only http/https URLs are supported."
    max_images = max(0, min(max_images, MAX_IMAGES_HARD_LIMIT))
    try:
        downloaded_html = await asyncio.to_thread(trafilatura.fetch_url, url)
    except Exception as exc:
        return f"Error downloading page: {exc}"
    if not downloaded_html:
        return "Failed to retrieve content from the page."

    text = trafilatura.extract(downloaded_html) or "Text extraction failed for this page."
    if not max_images:
        return text

    candidates = _extract_image_candidates(downloaded_html, url, SEARCH_IMAGES_HARD_LIMIT)
    fetched = await asyncio.gather(
        *(asyncio.to_thread(_fetch_and_process_image, str(c["url"])) for c in candidates)
    )
    images = []
    for meta, result in zip(candidates, fetched):
        if result is None:
            continue
        data, fmt, size = result
        images.append((meta, data, fmt, size))
        if len(images) >= max_images:
            break
    if not images:
        return text

    listing = "\n".join(
        f"{i}. {meta['alt'] or 'image'} ({size[0]}x{size[1]}): {meta['url']}"
        for i, (meta, _data, _fmt, size) in enumerate(images, 1)
    )
    header = f"{text}\n\n---\nImages on page ({len(images)}):\n{listing}"
    return [
        header,
        *(Image(data=data, format=fmt).to_image_content() for _meta, data, fmt, _size in images),
    ]

@mcp.tool(
    title="Get Page Content (clean text + images)",
    description="Extracts clean text from a webpage via trafilatura and also returns up to "
    "max_images important images from the page as MCP image content blocks "
    "(pass max_images=0 to get text only)."
)
async def get_page_content(url: str, max_images: int = DEFAULT_MAX_IMAGES) -> str | list[str | ImageContent]:
    """MCP-инструмент: чистый текст страницы + важные изображения с неё (как image-блоки)."""
    return await _get_page_content(url, max_images)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    """Проверка живости сервиса; не лимитируется и (при включённой авторизации) не требует токена."""
    return JSONResponse({"status": "ok"})


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Ограничивает частоту запросов с одного IP: не более max_requests за window_seconds (429 сверху)."""

    def __init__(self, app, max_requests: int, window_seconds: int):
        super().__init__(app)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.hits: DefaultDict[str, List[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        cutoff = now - self.window_seconds
        recent = [t for t in self.hits[ip] if t > cutoff]
        if len(recent) >= self.max_requests:
            return JSONResponse({"detail": "Too Many Requests"}, status_code=429)
        recent.append(now)
        self.hits[ip] = recent
        return await call_next(request)


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Проверяет токен из заголовка x-auth-token или Authorization: Bearer (сейчас отключён в app)."""

    def __init__(self, app, token: str):
        super().__init__(app)
        self.token = token.encode()

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        provided = request.headers.get("x-auth-token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            provided = provided or auth[7:].strip()
        if not secrets.compare_digest(provided.encode(), self.token):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)


app = mcp.http_app(
    middleware=[
        Middleware(RateLimitMiddleware, max_requests=RATE_LIMIT_MAX, window_seconds=RATE_LIMIT_WINDOW),
        # Middleware(TokenAuthMiddleware, token=EXPECTED_TOKEN),
    ],
    stateless_http=True, # True отключает сессии на стороне сервера (каждое обращение уникально)
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="debug")
