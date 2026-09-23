#!/usr/bin/env python3
"""
Germany News -> Threads autoposter

Парсит RSS-ленты политических новостей Германии, отбирает только новости по теме,
через Claude пишет короткий пост на русском и публикует его в Threads
через официальный Threads API. Запускается по расписанию в GitHub Actions.
"""

import os
import re
import json
import time
import random
import hashlib
import logging
from datetime import datetime, timezone

import feedparser
import requests
from anthropic import Anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("germany-threads-bot")

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
THREADS_ACCESS_TOKEN = os.environ["THREADS_ACCESS_TOKEN"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "1"))
MAX_CLAUDE_CHECKS_PER_RUN = int(os.environ.get("MAX_CLAUDE_CHECKS_PER_RUN", "6"))
MIN_INTERVAL_MINUTES = int(os.environ.get("MIN_INTERVAL_MINUTES", "60"))
MAX_INTERVAL_MINUTES = int(os.environ.get("MAX_INTERVAL_MINUTES", "150"))
STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")

THREADS_API = "https://graph.threads.net/v1.0"
THREADS_TEXT_LIMIT = 500

RSS_SOURCES = [
    {"name": "ARD Tagesschau - Inland", "url": "https://www.tagesschau.de/inland/index~rss2.xml"},
    {"name": "Deutschlandfunk - Politik", "url": "https://www.deutschlandfunk.de/politikportal-100.rss"},
    {"name": "ZDF - Politik", "url": "https://www.zdf.de/rss/zdf/nachrichten/politik"},
    {"name": "Süddeutsche Zeitung - Politik", "url": "https://rss.sueddeutsche.de/rss/Politik"},
]

# Дешёвый предварительный отсев по ссылке/заголовку (до вызова Claude)
BLOCK_WORDS = ["/sport/", "/wetter/", "fussball", "fußball", "bundesliga", "tatort", "lotto"]

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Состояние
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"seen_hashes": [], "seen_links": [], "next_post_not_before": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("seen_hashes", [])
    state.setdefault("seen_links", [])
    state.setdefault("next_post_not_before", None)
    return state


def save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    state["seen_hashes"] = state["seen_hashes"][-500:]
    state["seen_links"] = state["seen_links"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def mark_seen(state: dict, item: dict) -> None:
    state["seen_links"].append(item["link"])
    state["seen_hashes"].append(item["hash"])


def schedule_next_post(state: dict) -> None:
    delay = random.uniform(MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES)
    state["next_post_not_before"] = datetime.now(timezone.utc).timestamp() + delay * 60
    log.info(f"Следующий пост не раньше чем через {delay:.1f} мин")


def is_too_early(state: dict) -> bool:
    not_before = state.get("next_post_not_before")
    return bool(not_before) and datetime.now(timezone.utc).timestamp() < not_before


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------
def content_hash(title: str, summary: str) -> str:
    return hashlib.sha256((title + summary).encode("utf-8")).hexdigest()


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def extract_image(entry):
    for key in ("media_content", "media_thumbnail"):
        for m in entry.get(key, []) or []:
            url = m.get("url")
            if url and url.startswith("http"):
                return url
    for enc in entry.get("enclosures", []) or []:
        if enc.get("type", "").startswith("image") and enc.get("href"):
            return enc["href"]
    for link in entry.get("links", []) or []:
        if link.get("type", "").startswith("image") and link.get("href"):
            return link["href"]
    return None


def fetch_candidates(state: dict) -> list:
    candidates = []
    for source in RSS_SOURCES:
        try:
            feed = feedparser.parse(source["url"])
        except Exception as e:
            log.warning(f"Не удалось загрузить {source['name']}: {e}")
            continue
        if feed.bozo and not feed.entries:
            log.warning(f"Лента {source['name']} вернула ошибку без записей, пропускаю")
            continue

        for entry in feed.entries[:10]:
            link = entry.get("link", "")
            title = entry.get("title", "").strip()
            summary = strip_html(entry.get("summary", "") or entry.get("description", ""))[:800]
            if not link or not title:
                continue
            if link in state["seen_links"]:
                continue
            h = content_hash(title, summary)
            if h in state["seen_hashes"]:
                continue

            item = {
                "source": source["name"],
                "link": link,
                "title": title,
                "summary": summary,
                "image_url": extract_image(entry),
                "hash": h,
                "published": entry.get("published_parsed") and time.mktime(entry.published_parsed) or 0,
            }

            if any(w in (link + " " + title).lower() for w in BLOCK_WORDS):
                mark_seen(state, item)
                continue

            candidates.append(item)

    candidates.sort(key=lambda c: c["published"], reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------
def write_post(title: str, summary: str, source_name: str):
    prompt = f"""Ты ведёшь личный аккаунт в Threads: человек из Германии коротко и понятно рассказывает
русскоязычной аудитории о главных политических новостях страны.

СНАЧАЛА реши, подходит ли новость аккаунту.
Публикуем ТОЛЬКО: внутреннюю и внешнюю политику Германии, федеральное правительство и министров,
Бундестаг и Бундесрат, земельные парламенты и правительства, партии, выборы, законы и реформы,
миграцию и убежище, бюджет, налоги и экономическую политику, оборону и Бундесвер,
позицию и участие Германии в ЕС, НАТО и мировой политике.
НЕ публикуем: спорт, погоду, криминал, ДТП, пожары и происшествия без политического значения,
культуру, кино, музыку, шоу-бизнес, лайфстайл, здоровье и науку без политического решения,
новости других стран, если в них нет прямой связи с Германией или её политикой.
Если новость НЕ подходит — ответь ровно одним словом: SKIP

Если подходит — напиши пост для Threads на русском языке:
- первая строка — короткий цепляющий заголовок с одним эмодзи по теме (без markdown, без звёздочек)
- затем 2-3 коротких предложения по существу, нейтрально и понятно для тех, кто не живёт в Германии
- строго не длиннее 450 символов вместе с пробелами
- без хэштегов, ссылок, названия источника и подписи
- только текст поста, без пояснений

Новость на немецком (источник: {source_name}):
Заголовок: {title}
Описание: {summary}"""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        return text or None
    except Exception as e:
        log.error(f"Ошибка при обращении к Anthropic API: {e}")
        return None


def fit_limit(text: str) -> str:
    text = text.replace("*", "").strip().strip('"').strip()
    if len(text) <= THREADS_TEXT_LIMIT:
        return text
    cut = text[: THREADS_TEXT_LIMIT - 1]
    last_dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if last_dot > 200:
        return cut[: last_dot + 1]
    return cut.rstrip() + "…"


# ---------------------------------------------------------------------------
# Threads API
# ---------------------------------------------------------------------------
def threads_me():
    r = requests.get(
        f"{THREADS_API}/me",
        params={"fields": "id,username", "access_token": THREADS_ACCESS_TOKEN},
        timeout=30,
    )
    if r.status_code != 200:
        log.error(f"Токен Threads не работает ({r.status_code}): {r.text}")
        return None
    data = r.json()
    log.info(f"Threads-аккаунт: @{data.get('username')} (id {data.get('id')})")
    return data.get("id")


def create_container(user_id: str, text: str, image_url):
    params = {"access_token": THREADS_ACCESS_TOKEN, "text": text}
    if image_url:
        params.update({"media_type": "IMAGE", "image_url": image_url})
    else:
        params["media_type"] = "TEXT"
    r = requests.post(f"{THREADS_API}/{user_id}/threads", data=params, timeout=60)
    if r.status_code != 200:
        log.warning(f"Не удалось создать контейнер ({r.status_code}): {r.text}")
        return None
    return r.json().get("id")


def wait_until_ready(container_id: str, tries: int = 10) -> bool:
    for _ in range(tries):
        r = requests.get(
            f"{THREADS_API}/{container_id}",
            params={"fields": "status,error_message", "access_token": THREADS_ACCESS_TOKEN},
            timeout=30,
        )
        if r.status_code == 200:
            status = r.json().get("status")
            if status == "FINISHED":
                return True
            if status in ("ERROR", "EXPIRED"):
                log.warning(f"Контейнер не готов: {r.json()}")
                return False
        time.sleep(6)
    return False


def publish(user_id: str, container_id: str) -> bool:
    r = requests.post(
        f"{THREADS_API}/{user_id}/threads_publish",
        data={"creation_id": container_id, "access_token": THREADS_ACCESS_TOKEN},
        timeout=60,
    )
    if r.status_code != 200:
        log.error(f"Публикация не удалась ({r.status_code}): {r.text}")
        return False
    log.info(f"Опубликовано, id поста: {r.json().get('id')}")
    return True


def post_to_threads(user_id: str, text: str, image_url) -> bool:
    attempts = [image_url, None] if image_url else [None]
    for img in attempts:
        container_id = create_container(user_id, text, img)
        if not container_id:
            continue
        if not wait_until_ready(container_id):
            continue
        if publish(user_id, container_id):
            return True
    return False


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------
def main():
    log.info("Запуск Germany Threads Bot")
    state = load_state()

    if is_too_early(state):
        remaining = (state["next_post_not_before"] - datetime.now(timezone.utc).timestamp()) / 60
        log.info(f"Ещё не время для следующего поста (~{remaining:.0f} мин), завершение")
        return

    user_id = threads_me()
    if not user_id:
        raise SystemExit("Проверьте секрет THREADS_ACCESS_TOKEN: токен недействителен или истёк")

    candidates = fetch_candidates(state)
    save_state(state)
    log.info(f"Найдено {len(candidates)} новых кандидатов")

    posted = 0
    checks = 0
    for item in candidates:
        if posted >= MAX_POSTS_PER_RUN or checks >= MAX_CLAUDE_CHECKS_PER_RUN:
            break
        checks += 1
        log.info(f"Обрабатываю: [{item['source']}] {item['title'][:80]}")

        body = write_post(item["title"], item["summary"], item["source"])
        if not body:
            continue

        if body.strip().strip("*").strip().upper().startswith("SKIP"):
            log.info("Не по теме, пропускаю и запоминаю")
            mark_seen(state, item)
            save_state(state)
            continue

        if post_to_threads(user_id, fit_limit(body), item["image_url"]):
            mark_seen(state, item)
            posted += 1
            schedule_next_post(state)
            save_state(state)
        else:
            log.error("Не удалось опубликовать, попробую эту новость в следующий раз")
            break

    log.info(f"Готово. Опубликовано постов: {posted}")


if __name__ == "__main__":
    main()
