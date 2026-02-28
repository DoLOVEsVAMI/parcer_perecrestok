#!/usr/bin/env python3
"""Парсер нутриентов товаров с perekrestok.ru в формате БЗ."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener
import http.cookiejar

DEFAULT_ENDPOINTS = [
    "https://www.perekrestok.ru/api/customer/1.4.1.0/catalog/search?text={query}&page={page}&perPage={per_page}",
    "https://www.perekrestok.ru/api/customer/1.5.0.0/catalog/search?text={query}&page={page}&perPage={per_page}",
]
SEARCH_PAGE = "https://www.perekrestok.ru/cat/search?search={query}"


@dataclass
class ProductNutrition:
    title: str
    kcal: float
    protein: float
    fat: float
    carbs: float
    energy: int

    def as_bz_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "kcal": self.kcal,
            "protein": self.protein,
            "fat": self.fat,
            "carbs": self.carbs,
            "portion": 100,
            "energy": self.energy,
            "is_active": True,
        }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", ".")
        match = re.search(r"-?\d+(?:\.\d+)?", cleaned)
        return float(match.group(0)) if match else None
    return None


def _first_non_none(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in data:
            parsed = _to_float(data.get(key))
            if parsed is not None:
                return parsed
    return None


def _collect_dicts(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_collect_dicts(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_dicts(item))
    return found


def _extract_title(product: dict[str, Any]) -> str | None:
    for key in ("title", "name", "label", "productName"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_brand(product: dict[str, Any]) -> str | None:
    for key in ("brand", "brandName", "trademark"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _extract_nutrition(product: dict[str, Any]) -> ProductNutrition | None:
    title = _extract_title(product)
    if not title:
        return None

    for candidate in _collect_dicts(product):
        protein = _first_non_none(candidate, "protein", "proteins", "proteinAmount", "proteinValue", "belki")
        fat = _first_non_none(candidate, "fat", "fats", "fatAmount", "fatValue", "zhiry")
        carbs = _first_non_none(candidate, "carbs", "carbohydrates", "carbohydrate", "carbohydratesAmount", "uglevody")
        kcal = _first_non_none(candidate, "kcal", "calories", "caloriesKcal", "energyKcal", "kkal")
        energy_kj = _first_non_none(candidate, "energy", "energyKj", "energyKJ", "kilojoules", "kj")

        if protein is None or fat is None or carbs is None or kcal is None:
            continue
        if energy_kj is None:
            energy_kj = kcal * 4.184

        return ProductNutrition(
            title=title,
            kcal=round(kcal, 2),
            protein=round(protein, 2),
            fat=round(fat, 2),
            carbs=round(carbs, 2),
            energy=int(round(energy_kj)),
        )
    return None


class HttpClient:
    def __init__(self, timeout_s: int):
        self.timeout_s = timeout_s
        cookie_jar = http.cookiejar.CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(cookie_jar))
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "ru,en;q=0.9",
            "Referer": "https://www.perekrestok.ru/",
            "Origin": "https://www.perekrestok.ru",
            "Connection": "keep-alive",
        }

    def warmup(self) -> None:
        urls = [
            "https://www.perekrestok.ru/",
            SEARCH_PAGE.format(query=quote("хлеб")),
        ]
        for url in urls:
            try:
                req = Request(url, headers={**self.base_headers, "Accept": "text/html,*/*"})
                with self.opener.open(req, timeout=self.timeout_s):
                    pass
            except Exception:
                pass

    def fetch_json(self, url: str) -> dict[str, Any]:
        req = Request(url, headers=self.base_headers)
        with self.opener.open(req, timeout=self.timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))


class PlaywrightClient:
    def __init__(self, timeout_s: int):
        self.timeout_s = timeout_s

    async def fetch_json(self, url: str) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(locale="ru-RU")
            page = await context.new_page()
            await page.goto("https://www.perekrestok.ru/", wait_until="domcontentloaded", timeout=self.timeout_s * 1000)
            await page.wait_for_timeout(2500)

            result = await page.evaluate(
                """async (u) => {
                    const r = await fetch(u, {
                      method: 'GET',
                      credentials: 'include',
                      headers: {'accept': 'application/json,text/plain,*/*'}
                    });
                    const text = await r.text();
                    return {status: r.status, text};
                }""",
                url,
            )
            await browser.close()

        if result["status"] >= 400:
            raise RuntimeError(f"Playwright fetch status={result['status']}")
        return json.loads(result["text"])


def _fetch_json_with_fallback(url: str, http_client: HttpClient, use_playwright: bool) -> dict[str, Any]:
    try:
        return http_client.fetch_json(url)
    except HTTPError as exc:
        if exc.code != 403 or not use_playwright:
            raise
    except Exception:
        if not use_playwright:
            raise

    pw = PlaywrightClient(timeout_s=http_client.timeout_s)
    try:
        return asyncio.run(pw.fetch_json(url))
    except ModuleNotFoundError as exc:
        raise RuntimeError("Playwright не установлен. Установите: pip install playwright && python -m playwright install chromium") from exc


def collect_products_for_query(
    query: str,
    endpoint_templates: list[str],
    target_count: int,
    per_page: int,
    max_pages: int,
    timeout_s: int,
    sleep_s: float,
    use_playwright: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    seen_brands: set[str] = set()

    http_client = HttpClient(timeout_s=timeout_s)
    http_client.warmup()

    for page in range(1, max_pages + 1):
        payload: dict[str, Any] | None = None
        last_error: Exception | None = None
        for endpoint_template in endpoint_templates:
            url = endpoint_template.format(query=quote(query), page=page, per_page=per_page)
            try:
                payload = _fetch_json_with_fallback(url, http_client=http_client, use_playwright=use_playwright)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc

        if payload is None:
            print(f"  ! page {page}: {last_error}")
            break

        for obj in _collect_dicts(payload):
            nutrition = _extract_nutrition(obj)
            if nutrition is None:
                continue

            normalized_title = re.sub(r"\s+", " ", nutrition.title.lower()).strip()
            if normalized_title in seen_titles:
                continue

            brand = _extract_brand(obj)
            if brand and brand in seen_brands:
                continue

            results.append(nutrition.as_bz_json())
            seen_titles.add(normalized_title)
            if brand:
                seen_brands.add(brand)

            if len(results) >= target_count:
                return results

        time.sleep(sleep_s)

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Парсер нутриентов товаров Перекрёстка")
    parser.add_argument("--queries-file", default="categories.txt", help="Файл со списком запросов")
    parser.add_argument("--output", default="products.json", help="Куда сохранить JSON")
    parser.add_argument("--endpoint-template", action="append", dest="endpoint_templates")
    parser.add_argument("--target-per-query", type=int, default=10)
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--no-playwright", action="store_true", help="Отключить fallback через браузер")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries = [line.strip() for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    endpoint_templates = args.endpoint_templates if args.endpoint_templates else DEFAULT_ENDPOINTS

    output: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        items = collect_products_for_query(
            query=query,
            endpoint_templates=endpoint_templates,
            target_count=args.target_per_query,
            per_page=args.per_page,
            max_pages=args.max_pages,
            timeout_s=args.timeout,
            sleep_s=args.sleep,
            use_playwright=not args.no_playwright,
        )
        output[query] = items
        print(f"{query}: {len(items)}")

    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
