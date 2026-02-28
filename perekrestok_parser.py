#!/usr/bin/env python3
"""Парсер нутриентов товаров из Поиска Перекрёстка.

Скрипт пытается получить по 10 позиций для каждого запроса,
нормализует БЖУ на 100 г и формирует JSON в формате БЗ.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = (
    "https://www.perekrestok.ru/api/customer/1.4.1.0/catalog/search"
    "?text={query}&page={page}&perPage={per_page}"
)


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

    dict_candidates = _collect_dicts(product)
    for candidate in dict_candidates:
        protein = _first_non_none(
            candidate,
            "protein",
            "proteins",
            "proteinAmount",
            "proteinValue",
            "belki",
        )
        fat = _first_non_none(candidate, "fat", "fats", "fatAmount", "fatValue", "zhiry")
        carbs = _first_non_none(
            candidate,
            "carbs",
            "carbohydrates",
            "carbohydrate",
            "carbohydratesAmount",
            "uglevody",
        )
        kcal = _first_non_none(
            candidate,
            "kcal",
            "calories",
            "caloriesKcal",
            "energyKcal",
            "kkal",
        )
        energy_kj = _first_non_none(
            candidate,
            "energy",
            "energyKj",
            "energyKJ",
            "kilojoules",
            "kj",
        )

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


def _fetch_json(url: str, timeout_s: int) -> dict[str, Any]:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.perekrestok.ru/",
        },
    )
    with urlopen(req, timeout=timeout_s) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


def collect_products_for_query(
    query: str,
    endpoint_template: str,
    target_count: int,
    per_page: int,
    max_pages: int,
    timeout_s: int,
    sleep_s: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    seen_brands: set[str] = set()

    for page in range(1, max_pages + 1):
        url = endpoint_template.format(query=quote(query), page=page, per_page=per_page)
        try:
            payload = _fetch_json(url, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! page {page}: {exc}")
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
    parser.add_argument("--endpoint-template", default=DEFAULT_ENDPOINT)
    parser.add_argument("--target-per-query", type=int, default=10)
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--sleep", type=float, default=0.2)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    queries = [
        line.strip()
        for line in Path(args.queries_file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    output: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        items = collect_products_for_query(
            query=query,
            endpoint_template=args.endpoint_template,
            target_count=args.target_per_query,
            per_page=args.per_page,
            max_pages=args.max_pages,
            timeout_s=args.timeout,
            sleep_s=args.sleep,
        )
        output[query] = items
        print(f"{query}: {len(items)}")

    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
