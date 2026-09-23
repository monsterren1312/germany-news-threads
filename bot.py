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

NEWS_SLOTS = [8, 12, 15, 21]                                 # 4 новостных поста в день (часы по Германии)
ENGAGEMENT_HOUR = 19                                         # 1 пост на комментарии/подписку, с 19:00
MIN_GAP_MINUTES = 45                                         # минимум между любыми двумя постами
MIN_SCORE = int(os.environ.get("MIN_SCORE", "5"))            # ниже — даже лучшую новость не публикуем
POST_HOURS = (7, 23)                                         # ночью не публикуем
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
    state.setdefault("last_engagement_date", None)
    state.setdefault("engagement_history", [])
    state.setdefault("day", None)
    state.setdefault("news_done", 0)
    state.setdefault("last_post_ts", None)
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


def berlin_now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Berlin"))


def slots_due(hour: int) -> int:
    return sum(1 for h in NEWS_SLOTS if hour >= h)


def roll_day(state: dict) -> None:
    """Новый день — обнуляем счётчик. В первый запуск не догоняем пропущенные слоты."""
    now = berlin_now()
    today = now.strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["news_done"] = max(0, slots_due(now.hour) - 1)


def recently_posted(state: dict) -> bool:
    last = state.get("last_post_ts")
    return bool(last) and datetime.now(timezone.utc).timestamp() - last < MIN_GAP_MINUTES * 60


def record_post(state: dict) -> None:
    state["last_post_ts"] = datetime.now(timezone.utc).timestamp()


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
    prompt = f"""Ты пишешь посты для Threads-аккаунта «Типичная Германия» — русскоязычный автор,
живущий в Германии, подаёт новости страны так, что мимо невозможно пролистать.

ФОРМАТ — КОРОТКО И ОСТРО. Пост читается за 3 секунды и бьёт как пощёчина.

Эталоны (повторяй манеру, не темы):

Пример 1:
518 000 евро, чтобы депортировать 33 человека.
75 полицейских. По два на каждого.
Больше — в шапке профиля.

Пример 2:
Германия готовится к войне. Почти.
План — 1400 страниц. Колонну Бундесвера на учениях остановили протестующие.
Больше — в шапке профиля.

Пример 3:
С 2028 года чек на кассе — только цифровой.
Бумагу сэкономят. На новые кассы раскошелятся магазины.
Больше — в шапке профиля.

ПРАВИЛА ФОРМАТА:
- максимум 3 строки текста + строка «Больше — в шапке профиля.»
- весь пост до 220 символов вместе с пробелами — чем короче, тем лучше
- первая строка до 60 символов: главная цифра или самый абсурдный факт, сразу в лоб
- вторая (и третья) строка — добивание: контраст, сарказм, неудобная деталь или колкий вывод
- никаких объяснений, предыстории, «по данным», «как сообщается», названий ведомств без нужды
- никаких вопросов в стиле «А вы что думаете?» — только если вопрос сам по себе бьёт
- рубленые фразы, точки вместо запятых, ноль канцелярита
- без эмодзи, хэштегов, ссылок, названия источника, markdown и КАПСА

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


def write_engagement_post(history: list):
    """Ежедневный пост без новости: вызывает комментарии и подписки."""
    recent = "\n".join(f"- {h}" for h in history[-15:]) or "- (пока не было)"
    weekday = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][
        datetime.now(ZoneInfo("Europe/Berlin")).weekday()
    ]
    prompt = f"""Ты ведёшь личный аккаунт в Threads: человек, который живёт в Германии и рассказывает
русскоязычной аудитории о жизни и новостях страны. Сегодня {weekday}, вечер.

Напиши ОДИН пост, цель которого — максимум комментариев и новые подписчики. Выбери один из форматов:
- острый вопрос о жизни в Германии, на который у каждого есть мнение (бюрократия, деньги, немцы, язык, работа, жильё)
- «выбери одно»: два варианта, пусть отвечают в комментариях
- просьба поделиться историей («расскажите, как вы…»)
- «непопулярное мнение» о жизни в Германии с вопросом, согласны ли люди
- мини-опрос по итогам недели (если сегодня пятница, суббота или воскресенье)

Правила:
- первая строка — крючок с одним эмодзи
- живой разговорный тон, коротко: не длиннее 350 символов
- в конце — призыв ответить в комментариях и мягкий призыв подписаться
  (например: «Подписывайтесь, если тоже живёте в Германии — каждый день разбираю главное»)
- без хэштегов, ссылок, markdown и звёздочек
- никаких ложных обещаний: без розыгрышей, подарков и «секретов», которых нет
- не разжигай ненависть к народам, религиям и группам людей
- тема и формат НЕ должны повторять недавние посты:
{recent}

Ответь в формате:
ТЕМА: <3-6 слов о теме>
ПОСТ:
<текст поста>"""
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-5",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        topic, _, body = raw.partition("ПОСТ:")
        topic = topic.replace("ТЕМА:", "").strip() or "без темы"
        body = body.strip() or raw
        return topic, body
    except Exception as e:
        log.error(f"Ошибка при создании поста на вовлечение: {e}")
        return None, None


def maybe_post_engagement(state: dict) -> bool:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    today = now.strftime("%Y-%m-%d")
    if state.get("last_engagement_date") == today:
        return False
    if now.hour < ENGAGEMENT_HOUR:
        return False

    user_id = threads_me()
    if not user_id:
        raise SystemExit("Проверьте секрет THREADS_ACCESS_TOKEN: токен недействителен или истёк")

    topic, body = write_engagement_post(state["engagement_history"])
    if not body:
        return False
    log.info(f"Пост на вовлечение, тема: {topic}")
    if post_to_threads(user_id, fit_limit(body), None):
        state["last_engagement_date"] = today
        state["engagement_history"] = (state["engagement_history"] + [topic])[-30:]
        record_post(state)
        save_state(state)
        log.info("Готово: пост на вовлечение опубликован")
        return True
    return False


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
    now = berlin_now()

    if not (POST_HOURS[0] <= now.hour < POST_HOURS[1]):
        log.info(f"Сейчас {now.hour}:00 по Германии — ночью не публикуем")
        return

    roll_day(state)
    save_state(state)

    if recently_posted(state):
        log.info(f"Последний пост был меньше {MIN_GAP_MINUTES} мин назад, жду")
        return

    if maybe_post_engagement(state):
        return

    due = slots_due(now.hour)
    if state["news_done"] >= due:
        log.info(f"Новостных постов сегодня {state['news_done']}/{len(NEWS_SLOTS)}, следующий слот позже")
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
        log.info(f"Нет достаточно резонансных новостей (порог {MIN_SCORE}), попробую через час")
        return

    body = write_post(item["title"], item["summary"], item["source"])
    if not body:
        return
    if body.strip().strip("*").strip().upper().startswith("SKIP"):
        log.info("Новость нельзя подать корректно, пропускаю")
        mark_seen(state, item)
        save_state(state)
        return

    log.info("Текст поста:\n" + fit_limit(body))
    if post_to_threads(user_id, fit_limit(body), item["image_url"]):
        mark_seen(state, item)
        state["news_done"] += 1
        record_post(state)
        save_state(state)
        log.info(f"Готово: новостной пост {state['news_done']}/{len(NEWS_SLOTS)} за сегодня")
    else:
        log.error("Не удалось опубликовать, попробую в следующий раз")


if __name__ == "__main__":
    main()
