#!/usr/bin/env python3
"""
Germany News -> Threads autoposter

Собирает свежие новости Германии из крупных СМИ, через Claude выбирает самую
резонансную и обсуждаемую, пишет цепляющий пост на русском и публикует его в Threads
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
from zoneinfo import ZoneInfo

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

MIN_INTERVAL_MINUTES = int(os.environ.get("MIN_INTERVAL_MINUTES", "90"))
MAX_INTERVAL_MINUTES = int(os.environ.get("MAX_INTERVAL_MINUTES", "180"))
MIN_SCORE = int(os.environ.get("MIN_SCORE", "7"))          # порог «вирусности» 1-10
POST_HOURS = (7, 23)                                         # публикуем только с 7:00 до 23:00 по Германии
MAX_CANDIDATES_TO_SCORE = 40
STATE_FILE = os.environ.get("STATE_FILE", "state/seen.json")

THREADS_API = "https://graph.threads.net/v1.0"
THREADS_TEXT_LIMIT = 500

# Общие ленты крупных СМИ: политика, общество, деньги, скандалы, происшествия, звёзды
RSS_SOURCES = [
    {"name": "Tagesschau", "url": "https://www.tagesschau.de/index~rss2.xml"},
    {"name": "Spiegel", "url": "https://www.spiegel.de/schlagzeilen/index.rss"},
    {"name": "n-tv", "url": "https://www.n-tv.de/rss"},
    {"name": "Welt", "url": "https://www.welt.de/feeds/latest.rss"},
    {"name": "Focus", "url": "https://rss.focus.de/fol/XML/rss_folnews.xml"},
    {"name": "t-online", "url": "https://www.t-online.de/feed.rss"},
    {"name": "Süddeutsche Zeitung", "url": "https://rss.sueddeutsche.de/rss/Topthemen"},
]

# Явно скучное — отсекаем сразу, до вызова Claude
BLOCK_WORDS = ["/wetter/", "lotto", "horoskop", "gewinnspiel", "rezept", "liveblog", "live-ticker", "newsblog"]

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
    state["seen_hashes"] = state["seen_hashes"][-800:]
    state["seen_links"] = state["seen_links"][-800:]
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
        log.info(f"{source['name']}: {len(feed.entries)} записей")

        for entry in feed.entries[:15]:
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
def pick_best(candidates: list):
    """Одним запросом просит Claude оценить резонансность заголовков и вернуть лучший."""
    batch = candidates[:MAX_CANDIDATES_TO_SCORE]
    lines = "\n".join(f"{i}. [{c['source']}] {c['title']}" for i, c in enumerate(batch))
    prompt = f"""Ты редактор вирусного русскоязычного аккаунта в Threads о жизни в Германии.
Аудитория — русскоязычные, которые живут в Германии или интересуются ею.

Оцени каждую новость по шкале 1-10: насколько она вызовет эмоции, споры и репосты у этой аудитории.
Высокие оценки: скандалы, возмущение, неожиданные решения властей, деньги (налоги, пособия,
Bürgergeld, пенсии, штрафы, цены, аренда), миграция и документы, громкие происшествия,
абсурдные истории из немецкой жизни, скандалы со знаменитостями и политиками, всё, что «касается каждого».
Низкие оценки: сухая протокольная политика, мелкие региональные события, спортивные результаты,
культура без скандала, иностранные новости без связи с Германией.

Ответь ТОЛЬКО JSON без пояснений: {{"scores": {{"0": 5, "1": 8, ...}}}}

Новости:
{lines}"""
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in response.content if hasattr(b, "text"))
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        scores = {int(k): int(v) for k, v in json.loads(raw)["scores"].items()}
        scores = {k: v for k, v in scores.items() if 0 <= k < len(batch)}
    except Exception as e:
        log.error(f"Не удалось получить оценки от Claude: {e}")
        return None, {}
    best = max(scores, key=scores.get, default=None)
    return best, scores


def write_post(title: str, summary: str, source_name: str):
    prompt = f"""Ты ведёшь личный аккаунт в Threads: человек, который живёт в Германии,
рассказывает русскоязычной аудитории самые обсуждаемые новости страны. Задача — чтобы пост
остановил скролл и под ним начали спорить в комментариях.

Напиши пост на русском:
- первая строка — сильный крючок с одним эмодзи: самое удивительное, возмутительное или
  неожиданное в этой новости (без markdown, без звёздочек, без КАПСА целыми словами)
- затем 1-2 коротких предложения: что произошло и почему это касается людей
- последняя строка — короткий вопрос к аудитории, на который хочется ответить
- живой разговорный тон, как пишет человек, а не агентство
- строго не длиннее 450 символов вместе с пробелами
- без хэштегов, ссылок, названия источника и подписи

Жёсткие правила:
- только факты из новости ниже; не выдумывай детали, цифры и цитаты, не преувеличивай
- не называй имён частных лиц — пострадавших и подозреваемых
- не разжигай ненависть к народам, религиям и другим группам людей
Если новость нельзя подать без нарушения этих правил — ответь одним словом: SKIP

Новость на немецком (источник: {source_name}):
Заголовок: {title}
Описание: {summary}

Ответь только текстом поста."""

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

    hour = datetime.now(ZoneInfo("Europe/Berlin")).hour
    if not (POST_HOURS[0] <= hour < POST_HOURS[1]):
        log.info(f"Сейчас {hour}:00 по Германии — ночью не публикуем, завершение")
        return

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
    if not candidates:
        return

    best, scores = pick_best(candidates)
    if best is None:
        return

    # Слабые новости запоминаем, чтобы не оценивать их повторно
    for i, sc in scores.items():
        if sc < 5:
            mark_seen(state, candidates[i])
    save_state(state)

    item = candidates[best]
    score = scores[best]
    log.info(f"Лучшая новость ({score}/10): [{item['source']}] {item['title'][:90]}")
    if score < MIN_SCORE:
        log.info(f"Нет достаточно резонансных новостей (порог {MIN_SCORE}), жду следующего запуска")
        return

    body = write_post(item["title"], item["summary"], item["source"])
    if not body:
        return
    if body.strip().strip("*").strip().upper().startswith("SKIP"):
        log.info("Новость нельзя подать корректно, пропускаю")
        mark_seen(state, item)
        save_state(state)
        return

    if post_to_threads(user_id, fit_limit(body), item["image_url"]):
        mark_seen(state, item)
        schedule_next_post(state)
        save_state(state)
        log.info("Готово: пост опубликован")
    else:
        log.error("Не удалось опубликовать, попробую в следующий раз")


if __name__ == "__main__":
    main()
