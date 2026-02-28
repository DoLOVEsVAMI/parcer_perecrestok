## Парсер позиций из Перекрёстка

Скрипт `perekrestok_parser.py` собирает товары по поисковым запросам и формирует JSON в формате БЗ:

```json
{
  "title": "...",
  "kcal": 301.0,
  "protein": 11.7,
  "fat": 13.6,
  "carbs": 32.6,
  "portion": 100,
  "energy": 1259,
  "is_active": true
}
```

Если в источнике нет энергии в кДж, значение считается по формуле:
`energy = round(kcal * 4.184)`.

## Как это теперь работает

1. Сначала обычный HTTP-клиент делает прогрев с cookie и пробует API поиска.
2. Если API отвечает `403`, автоматически включается fallback через headless-браузер Playwright и тот же API вызывается из контекста страницы.
3. Дубликаты отсекаются по нормализованному названию и по бренду.

## Запуск

```bash
python perekrestok_parser.py \
  --queries-file categories.txt \
  --output products.json \
  --target-per-query 10
```

### Полезные опции

- `--endpoint-template` — добавить свой шаблон endpoint (можно передать несколько раз);
- `--no-playwright` — отключить fallback через браузер;
- `--max-pages` — глубина пагинации;
- `--per-page` — сколько карточек брать с одной страницы поиска.

## Установка Playwright (рекомендуется)

```bash
pip install playwright
python -m playwright install chromium
```

Без Playwright скрипт всё равно работает, но при жёстком антиботе/403 может не получить результаты.
