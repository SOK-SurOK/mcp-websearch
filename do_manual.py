"""Ручной запуск функций MCP-сервера для проверки их работы без поднятия сервера.

Примеры:
    python do_manual.py search "python asyncio tutorial" -n 5
    python do_manual.py page "https://en.wikipedia.org/wiki/Python_(programming_language)" -n 2
    python do_manual.py page https://www.perm.kp.ru/daily/277819.5/5307904/ -n 0 -o my_results
    https://59.ru/text/autumn/2026/09/27/76665026/
"""

import argparse
import asyncio
import base64
from datetime import datetime
from pathlib import Path

from mcp.types import ImageContent

from main import _get_page_content, _websearch


def save_result(result, out_dir: Path) -> None:
    """Сохраняет результат в out_dir: текст — в result.txt, изображения — в img<N>.<ext>."""
    out_dir.mkdir(parents=True, exist_ok=True)

    if isinstance(result, str):
        path = out_dir / "result.txt"
        path.write_text(result, encoding="utf-8")
        print(f"Текст: {path} ({len(result)} символов)")
        return

    img_num = 0
    for block in result:
        if isinstance(block, ImageContent):
            img_num += 1
            ext = "png" if block.mimeType == "image/png" else "jpg"
            path = out_dir / f"img{img_num}.{ext}"
            path.write_bytes(base64.b64decode(block.data))
            print(f"Изображение: {path} ({block.mimeType})")
        else:
            path = out_dir / "result.txt"
            path.write_text(str(block), encoding="utf-8")
            print(f"Текст: {path} ({len(str(block))} символов)")
    if not img_num:
        print("Изображений на странице не найдено.")


async def run(args: argparse.Namespace) -> None:
    # каждый запуск складывается в свою подпапку: tmp/search_20260927_210530/
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.command == "search":
        result = await _websearch(args.query, args.max_results)
        save_result(result, args.out / f"search_{stamp}")
    else:
        result = await _get_page_content(args.url, args.max_images)
        save_result(result, args.out / f"page_{stamp}")


def parse_args() -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-o", "--out", type=Path, default=Path("tmp"),
        help="папка для сохранения результатов (по умолчанию tmp)",
    )

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", parents=[common], help="поиск в DuckDuckGo")
    s.add_argument("query", help="поисковый запрос")
    s.add_argument("-n", "--max-results", type=int, default=3, help="макс. число результатов")

    p = sub.add_parser("page", parents=[common], help="извлечь контент страницы")
    p.add_argument("url", help="URL страницы")
    p.add_argument(
        "-n", "--max-images", type=int, default=3,
        help="макс. число изображений (0 — только текст)",
    )

    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args()))
