from __future__ import annotations

import csv
import html as html_lib
import json
import math
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

SHEET_ID = "1aoVhmKbdwQm9oqJ5980dbCy86wMbdLuUVtzuJalG824"
SHEET_EXPORT = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)
OUTPUT_CSV = OUTPUT_DIR / "dari_sku_2000_20260901.csv"
STATUS_JSON = OUTPUT_DIR / "dari_sku_2000_20260901_status.json"
TARGET = 2000
OPTIONS = (1, 2, 3, 5, 10)
START_SKU = 654736

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

STOPWORDS = {
    "with", "and", "the", "for", "from", "per", "each", "free", "natural",
    "organic", "tablets", "tablet", "capsules", "capsule", "softgels", "softgel",
    "gummies", "gummy", "powder", "liquid", "vegetarian", "vegan", "count", "pack",
    "개", "정", "캡슐", "정제", "구미", "분말", "액상", "유기농", "천연", "함유",
}


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(str(value or ""))).strip()


def as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(str(value).replace(",", "").replace("₩", "").strip())))
    except Exception:
        return None


def recursive_json(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from recursive_json(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from recursive_json(value)


def extract_product_json(soup: BeautifulSoup) -> dict[str, Any]:
    best: dict[str, Any] = {}
    for node in soup.select('script[type="application/ld+json"]'):
        raw = node.string or node.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for item in recursive_json(data):
            typ = item.get("@type")
            types = typ if isinstance(typ, list) else [typ]
            if "Product" in types:
                if len(json.dumps(item, ensure_ascii=False)) > len(json.dumps(best, ensure_ascii=False)):
                    best = item
    return best


def normalize_images(value: Any) -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        urls.append(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                for key in ("url", "contentUrl"):
                    if isinstance(item.get(key), str):
                        urls.append(item[key])
    elif isinstance(value, dict):
        for key in ("url", "contentUrl"):
            if isinstance(value.get(key), str):
                urls.append(value[key])
    out: list[str] = []
    for url in urls:
        url = clean_text(url)
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http") and url not in out:
            out.append(url)
    return out


def product_id_from_url(url: str) -> str:
    match = re.search(r"/(\d+)(?:[/?#]|$)", url)
    return match.group(1) if match else ""


def fetch_candidates() -> tuple[list[dict[str, str]], str]:
    rows: list[list[str]] = []
    source = "google_sheet_export"
    try:
        response = requests.get(SHEET_EXPORT, headers={"User-Agent": UA}, timeout=60)
        response.raise_for_status()
        text = response.content.decode("utf-8-sig", errors="replace")
        if "accounts.google.com" in response.url or "<!doctype html" in text[:500].lower():
            raise RuntimeError("sheet export requires authentication")
        rows = list(csv.reader(text.splitlines()))
    except Exception as exc:
        source = f"fallback_public_catalog:{type(exc).__name__}"

    candidates: list[dict[str, str]] = []
    if rows:
        for row in rows:
            if len(row) < 16:
                row += [""] * (16 - len(row))
            status, sku, category, name, _, _, url, _, _, _, _, _, _, english, brand, country = row[:16]
            try:
                sku_num = int(float(sku))
            except Exception:
                continue
            if not (START_SKU <= sku_num < START_SKU + TARGET):
                continue
            if "iherb.com" not in url.lower():
                continue
            candidates.append({
                "product_id": product_id_from_url(url),
                "url": url.strip(),
                "name_en": clean_text(english or name),
                "brand": clean_text(brand),
                "category_seed": clean_text(category),
                "country_seed": clean_text(country),
            })

    if len(candidates) == TARGET:
        return candidates, source

    # 공개 원천 데이터에서 후보를 재구성한다. CAPTCHA나 인증 우회는 하지 않는다.
    public_sources = [
        "https://raw.githubusercontent.com/suppsaudit/my-supps/79646b236837d6cee7b8bfe16946223afc5da654/iherb_json_products.csv",
        "https://raw.githubusercontent.com/suppsaudit/my-supps/79646b236837d6cee7b8bfe16946223afc5da654/kaggle_iherb_supplements.csv",
        "https://raw.githubusercontent.com/Akintayopope/Prairie_Naturals/f326a6725ebac7ccda4062d3345b644f99d7eb8e/db/data/iherb_products.csv",
    ]
    seen: set[str] = set()
    fallback: list[dict[str, str]] = []
    for src in public_sources:
        try:
            resp = requests.get(src, headers={"User-Agent": UA}, timeout=60)
            resp.raise_for_status()
            text = resp.content.decode("utf-8-sig", errors="replace")
            reader = csv.DictReader(text.splitlines())
            for item in reader:
                values = {str(k or "").lower(): clean_text(v) for k, v in item.items()}
                url = next((v for k, v in values.items() if "url" in k and "iherb" in v.lower()), "")
                pid = next((v for k, v in values.items() if k in {"product_id", "id", "pid", "productid"} and v.isdigit()), "")
                if not pid and url:
                    pid = product_id_from_url(url)
                if not pid or pid in seen:
                    continue
                if not url:
                    url = f"https://kr.iherb.com/pr/{pid}"
                name = next((v for k, v in values.items() if k in {"name", "product_name", "title", "name_en"} and v), "")
                brand = next((v for k, v in values.items() if "brand" in k and v), "")
                if not name:
                    continue
                if brand.lower() in {"now foods", "california gold nutrition", "kunna"}:
                    continue
                seen.add(pid)
                fallback.append({
                    "product_id": pid,
                    "url": url,
                    "name_en": name,
                    "brand": brand or name.split(",", 1)[0],
                    "category_seed": "",
                    "country_seed": "",
                })
                if len(fallback) >= TARGET:
                    break
        except Exception:
            continue
        if len(fallback) >= TARGET:
            break
    if len(fallback) < TARGET:
        raise RuntimeError(f"candidate count insufficient: {len(fallback)}")
    return fallback[:TARGET], source


def extract_size(name: str) -> str:
    text = clean_text(name)
    patterns = [
        r"\((\d+(?:\.\d+)?\s*(?:kg|g|mg|mcg|ml|L))\)",
        r"(\d+(?:\.\d+)?\s*(?:fl\s*oz|oz|lb|kg|g|ml|L))",
        r"(\d+\s*(?:Tablets|Capsules|Softgels|Soft Gels|Gummies|Pieces|Tea Bags|Packets|Bars|Chews|Doses|Count))",
        r"(\d+\s*(?:정|캡슐|소프트젤|구미|개입|티백|포|스틱|회분))",
    ]
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(re.findall(pattern, text, flags=re.I))
    if matches:
        # 마지막에 표기되는 미터법 중량을 우선한다.
        metric = [m for m in matches if re.search(r"\b(?:kg|g|ml|L)\b", m, flags=re.I)]
        return clean_text(metric[-1] if metric else matches[-1])
    return "옵션 확인"


def classify(name: str, seed: str) -> str:
    s = (name + " " + seed).lower()
    food_words = ("tea", "coffee", "sardine", "mackerel", "tuna", "pasta", "spaghetti", "noodle", "ramen", "beans", "lentil", "oats", "flour", "cookie", "bar", "snack", "candy", "gum", "mint", "ghee", "butter", "oil", "soup", "broth", "honey", "salt", "spread", "granola", "rice")
    is_food = "식품" in seed and "보조" not in seed or any(w in s for w in food_words)
    if is_food:
        rules = [
            (("tea", "티백"), "식품>차/티백"),
            (("coffee",), "식품>커피"),
            (("sardine", "mackerel", "tuna"), "식품>수산물/통조림"),
            (("pasta", "spaghetti", "noodle", "ramen"), "식품>면류/파스타"),
            (("beans", "lentil", "chickpea"), "식품>콩/통조림"),
            (("oats", "flour", "rice"), "식품>곡물/가루"),
            (("cookie", "bar", "snack", "candy", "gum", "mint", "granola"), "식품>간식/스낵"),
            (("ghee", "butter", "oil"), "식품>오일/버터"),
            (("soup", "broth"), "식품>수프/육수"),
            (("honey",), "식품>꿀"),
            (("salt",), "식품>소금/조미료"),
            (("spread", "butter"), "식품>잼/스프레드"),
        ]
        for words, category in rules:
            if any(w in s for w in words):
                return category
        return "식품>기타"
    rules = [
        (("magnesium", "calcium", "zinc", "potassium", "mineral"), "건강식품>미네랄"),
        (("vitamin", "multivitamin", "b-complex", "b complex"), "건강식품>비타민"),
        (("probiotic", "dophilus", "lactobacillus"), "건강식품>유산균"),
        (("collagen",), "건강식품>콜라겐"),
        (("omega", "fish oil", "krill"), "건강식품>오메가3"),
        (("melatonin", "sleep"), "건강식품>수면건강"),
        (("protein", "creatine", "amino", "citrulline", "arginine"), "건강식품>스포츠영양"),
        (("ashwagandha", "herb", "extract"), "건강식품>허브/추출물"),
        (("enzyme",), "건강식품>효소"),
        (("coq10", "ubiquinol"), "건강식품>코엔자임Q10"),
    ]
    for words, category in rules:
        if any(w in s for w in words):
            return category
    return "건강식품>기타 영양제"


def build_tags(name_ko: str, name_en: str, brand: str, category: str) -> str:
    raw = f"{brand} {name_ko} {name_en} {category} 아이허브 해외직구"
    words = re.findall(r"[A-Za-z0-9가-힣]+", raw)
    tags: list[str] = []
    for word in words:
        if len(word) < 2 or word.lower() in STOPWORDS:
            continue
        normalized = word.replace(" ", "")
        if normalized not in tags:
            tags.append(normalized)
        if len(tags) >= 12:
            break
    return ",".join(tags)


def parse_origin(text: str) -> str:
    patterns = [
        r"(?:원산지|제조국|제조 국가)\s*[:：]?\s*([A-Za-z가-힣][A-Za-z가-힣 .-]{1,40})",
        r"Country of Origin\s*[:：]?\s*([A-Za-z][A-Za-z .-]{1,40})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if match:
            result = clean_text(match.group(1)).split("\n", 1)[0]
            result = re.split(r"(?:Disclaimer|면책|상품 설명|유통기한)", result, maxsplit=1, flags=re.I)[0]
            if 1 < len(result) <= 40:
                return result
    return "미확인"


def scrape_one(candidate: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = dict(candidate)
    result.update({"name_ko": "", "price": None, "main_image": "", "label_image": "", "origin": candidate.get("country_seed") or "미확인", "error": ""})
    url = candidate["url"] or f"https://kr.iherb.com/pr/{candidate['product_id']}"
    headers = {
        "User-Agent": UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    }
    session = requests.Session()
    for attempt in range(3):
        try:
            response = session.get(url, headers=headers, timeout=45, allow_redirects=True)
            if response.status_code in {403, 429}:
                raise RuntimeError(f"HTTP {response.status_code}")
            response.raise_for_status()
            html = response.text
            if re.search(r"captcha|verify you are human|길게 누르", html, flags=re.I):
                raise RuntimeError("challenge page")
            soup = BeautifulSoup(html, "html.parser")
            product = extract_product_json(soup)
            meta_title = soup.select_one('meta[property="og:title"]')
            h1 = soup.select_one("h1")
            name_ko = clean_text((h1.get_text(" ", strip=True) if h1 else "") or (meta_title.get("content") if meta_title else "") or product.get("name") or candidate.get("name_en"))
            name_ko = re.sub(r"\s*[|｜-]\s*iHerb.*$", "", name_ko, flags=re.I).strip()
            brand_obj = product.get("brand")
            brand = candidate.get("brand") or ""
            if isinstance(brand_obj, dict):
                brand = clean_text(brand_obj.get("name") or brand)
            elif isinstance(brand_obj, str):
                brand = clean_text(brand_obj or brand)
            images = normalize_images(product.get("image"))
            og_image = soup.select_one('meta[property="og:image"]')
            if og_image and og_image.get("content"):
                images = normalize_images([og_image.get("content"), *images])
            # 갤러리 이미지 중 영양/라벨 후보를 추가한다.
            for img in soup.select("img[src], img[data-src]"):
                src = img.get("data-src") or img.get("src")
                alt = clean_text(img.get("alt"))
                if src and re.search(r"supplement|nutrition|label|facts|성분|영양", alt + " " + src, flags=re.I):
                    for normalized in normalize_images(src):
                        if normalized not in images:
                            images.append(normalized)
            offers = product.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers]
            price = None
            for offer in offer_list:
                if isinstance(offer, dict):
                    price = as_int(offer.get("price") or offer.get("lowPrice"))
                    if price:
                        break
            if not price:
                for selector in ('meta[itemprop="price"]', 'meta[property="product:price:amount"]'):
                    node = soup.select_one(selector)
                    if node:
                        price = as_int(node.get("content"))
                        if price:
                            break
            if not price:
                match = re.search(r"₩\s*([\d,]+)", soup.get_text(" ", strip=True))
                price = as_int(match.group(1)) if match else None
            origin = parse_origin(soup.get_text("\n", strip=True))
            result.update({
                "url": response.url,
                "name_ko": name_ko,
                "brand": brand or name_ko.split(",", 1)[0],
                "price": price,
                "main_image": images[0] if images else "",
                "label_image": images[1] if len(images) > 1 else "",
                "origin": origin if origin != "미확인" else result["origin"],
            })
            return result
        except Exception as exc:
            result["error"] = clean_text(exc)
            time.sleep(1.2 * (attempt + 1))
    return result


def round_up(value: float) -> int:
    return int(math.ceil(value))


def sale_price(total_cost: int | None) -> int | str:
    if not total_cost:
        return ""
    p1 = round_up(total_cost / (1 - 0.2 - 0.1166))
    p2 = round_up((total_cost + 3001) / (1 - 0.1166))
    return max(p1, p2)


def main() -> int:
    candidates, source = fetch_candidates()
    scraped: list[dict[str, Any] | None] = [None] * len(candidates)
    workers = max(2, min(int(os.environ.get("SCRAPE_WORKERS", "8")), 12))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(scrape_one, c): i for i, c in enumerate(candidates)}
        for done, future in enumerate(as_completed(future_map), start=1):
            idx = future_map[future]
            try:
                scraped[idx] = future.result()
            except Exception as exc:
                failed = dict(candidates[idx])
                failed.update({"name_ko": candidates[idx]["name_en"], "price": None, "main_image": "", "label_image": "", "origin": "미확인", "error": clean_text(exc)})
                scraped[idx] = failed
            if done % 100 == 0:
                print(f"scraped {done}/{len(candidates)}", flush=True)

    headers = [
        "업로드 현황", "SKU", "Category", "Product_Korean", "옵션 중량(g), 개", "#tag",
        "Purchase link", "쿠팡 판매 등록가", "메인사진", "라벨사진(영양정보)", "배송비", "매입가",
        "원가총액", "영문 상품명", "영문 브랜드", "제조 국가",
    ]
    output_rows: list[list[Any]] = []
    sku = START_SKU
    success_price = success_image = success_ko = 0
    failed_products = 0
    for item in scraped:
        assert item is not None
        if item.get("price"):
            success_price += 1
        if item.get("main_image"):
            success_image += 1
        if re.search(r"[가-힣]", item.get("name_ko", "")):
            success_ko += 1
        if item.get("error"):
            failed_products += 1
        name_en = clean_text(item.get("name_en"))
        name_ko = clean_text(item.get("name_ko") or name_en)
        if not name_ko.endswith("사은품 추가증정"):
            name_ko = name_ko + " 사은품 추가증정"
        brand = clean_text(item.get("brand") or name_en.split(",", 1)[0])
        category = classify(name_en + " " + name_ko, item.get("category_seed", ""))
        size = extract_size(name_en or name_ko)
        tags = build_tags(name_ko, name_en, brand, category)
        base_price = as_int(item.get("price"))
        for qty in OPTIONS:
            purchase_cost = base_price * qty if base_price else None
            shipping = 10000
            total = shipping + purchase_cost if purchase_cost else None
            output_rows.append([
                "후보검토대기",
                sku,
                category,
                name_ko,
                f"{size}, {qty}개",
                tags,
                item.get("url") or f"https://kr.iherb.com/pr/{item.get('product_id')}",
                sale_price(total),
                item.get("main_image", ""),
                item.get("label_image", ""),
                shipping,
                purchase_cost or "",
                total or "",
                name_en,
                brand,
                clean_text(item.get("origin") or "미확인"),
            ])
            sku += 1

    with OUTPUT_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerows(output_rows)

    status = {
        "candidate_source": source,
        "source_products": len(candidates),
        "output_rows": len(output_rows),
        "start_sku": START_SKU,
        "end_sku": sku - 1,
        "price_found": success_price,
        "main_image_found": success_image,
        "korean_name_found": success_ko,
        "scrape_failures_or_partial": failed_products,
        "output_csv": str(OUTPUT_CSV),
    }
    STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
    if len(output_rows) != TARGET * len(OPTIONS):
        raise RuntimeError("unexpected output row count")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
