#!/usr/bin/env python3
"""
Грант-радар: обходит источники (страницы фондов и Telegram-каналы), находит
НОВЫЕ гранты/конкурсы по ключевым словам и присылает их в Telegram.

Память о том, что уже показывал, лежит в seen.json (workflow коммитит его
обратно в репозиторий).

Почему так устроено:
  • Многие сайты фондов рендерятся через JS или отдают 403 не-браузеру
    (rscf.ru/contests, fasie.ru), поэтому надёжнее брать:
      – статичные страницы-новости (type: html) — диффим по тексту заголовков;
      – Telegram-каналы через веб-превью t.me/s/<канал> (type: telegram) —
        чистый HTML, у каждого поста есть уникальная ссылка (по ней и диффим).
  • Не завязываемся на хрупкие CSS-селекторы конкретных сайтов.
"""

import datetime
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
SEEN_PATH = ROOT / "seen.json"
PENDING_PATH = ROOT / "pending.json"  # очередь находок для режима digest
ARCHIVE_PATH = ROOT / "archive.json"  # исторический архив всех находок
REPORT_PATH = ROOT / "archive.md"     # человекочитаемый отчёт (--report)
BOT_STATE_PATH = ROOT / "bot_state.json"  # offset getUpdates для бота-поиска

# «Браузерный» набор заголовков — часть сайтов иначе отвечает 403.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"⚠️  Не смог прочитать {path.name}, использую default")
    return default


def norm(text):
    return re.sub(r"\s+", " ", text.lower()).strip()


def md5(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def clean(text):
    """Схлопываем спам-повторы эмодзи ("🎤 🎤 🎤" -> "🎤") и лишние пробелы."""
    text = re.sub(r"(\S+)(?:\s+\1){2,}", r"\1", text)   # повторы токенов
    text = re.sub(r"(\S)\1{2,}", r"\1", text)           # повторы одного символа
    return " ".join(text.split()).strip()


def shorten(text, limit):
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def collect_html(html, base_url, cfg):
    """Заголовки со статичной страницы (ссылки, h1..h5, li).

    Возвращает список item-словарей: title(показ), text(для фильтра),
    link, key(для дедупа — по нормализованному тексту).
    """
    soup = BeautifulSoup(html, "html.parser")
    min_len = cfg.get("min_title_len", 15)
    max_len = cfg.get("max_title_len", 240)

    seen_norm = set()
    items = []
    for tag in soup.find_all(["a", "h1", "h2", "h3", "h4", "h5", "li"]):
        title = " ".join(tag.get_text(" ", strip=True).split())
        if not (min_len <= len(title) <= max_len):
            continue
        n = norm(title)
        if n in seen_norm:
            continue
        seen_norm.add(n)
        href = tag.get("href") if tag.name == "a" else None
        if not href:
            a = tag.find("a", href=True)
            href = a["href"] if a else None
        link = urljoin(base_url, href) if href else base_url
        items.append({"title": title, "text": title, "link": link, "key": n})
    return items


def collect_telegram(html, cfg):
    """Посты Telegram-канала из веб-превью t.me/s/<канал>.

    Для фильтра берём ПОЛНЫЙ текст поста, для показа — укороченный.
    Дедуп — по уникальной ссылке на пост.
    """
    soup = BeautifulSoup(html, "html.parser")
    limit = cfg.get("max_title_len", 240)
    items = []
    for msg in soup.select("div.tgme_widget_message"):
        body = msg.select_one("div.tgme_widget_message_text")
        if not body:
            continue
        text = body.get_text(" ", strip=True)
        if len(text) < cfg.get("min_title_len", 15):
            continue
        post = msg.get("data-post")  # вида "channel/123"
        link = f"https://t.me/{post}" if post else "https://t.me/"
        items.append(
            {"title": shorten(text, limit), "text": text, "link": link, "key": link}
        )
    return items


def matches(text, cfg):
    low = text.lower()
    if not any(k.lower() in low for k in cfg["keywords"]):
        return False
    if cfg.get("require_signal_word", True):
        return any(s.lower() in low for s in cfg.get("signal_words", []))
    return True


def matched_keywords(text, cfg):
    """Какие ключевые слова (направления) сработали в тексте."""
    low = text.lower()
    return [k for k in cfg["keywords"] if k.lower() in low]


def today_iso():
    return datetime.date.today().isoformat()


def tg_send(text, chat_id=None, reply_markup=None):
    target = chat_id or CHAT_ID
    if not (TOKEN and target):
        print("⚠️  Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — в консоль:\n" + text + "\n")
        return
    data = {
        "chat_id": target,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=30
    )
    if not r.ok:
        print(f"⚠️  Telegram вернул {r.status_code}: {r.text}")


def tg_answer_callback(callback_id):
    """Гасит «часики» на нажатой кнопке."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            data={"callback_query_id": callback_id},
            timeout=15,
        )
    except Exception:
        pass


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def scan(cfg):
    """Обходит все источники и возвращает список (src, item) по фильтру."""
    found = []
    for src in cfg["sources"]:
        try:
            html = fetch(src["url"])
        except Exception as e:
            print(f"⚠️  {src['name']}: не открылся ({e})")
            continue
        if src.get("type") == "telegram":
            items = collect_telegram(html, cfg)
        else:
            items = collect_html(html, src["url"], cfg)
        hits = [it for it in items if matches(it["text"], cfg)]
        print(f"• {src['name']}: элементов {len(items)}, по фильтру {len(hits)}")
        for it in hits:
            found.append((src, it))
    return found


def stream_item_msg(src, it):
    return (
        f"{src.get('emoji', '📌')} <b>{esc(it['title'])}</b>\n"
        f"<i>{esc(src['name'])}</i>\n{esc(it['link'])}"
    )


TG_LIMIT = 3500  # с запасом от лимита Telegram в 4096 символов


def build_digest(pending):
    """Собирает накопленные находки в дайджест-сообщения (с разбивкой по лимиту).

    Группирует по источнику; возвращает список готовых HTML-строк.
    """
    by_source = {}
    order = []
    for it in pending:
        name = it["name"]
        if name not in by_source:
            by_source[name] = {"emoji": it.get("emoji", "📌"), "items": []}
            order.append(name)
        by_source[name]["items"].append(it)

    header = f"🗂 <b>Грант-дайджест</b> — {len(pending)} новых за период\n"
    blocks = []
    for name in order:
        grp = by_source[name]
        lines = [f"\n{grp['emoji']} <b>{esc(name)}</b>"]
        for it in grp["items"]:
            lines.append(f"• <a href=\"{esc(it['link'])}\">{esc(it['title'])}</a>")
        blocks.append("\n".join(lines))

    # Склеиваем блоки в сообщения, не превышая лимит.
    messages = []
    cur = header
    for block in blocks:
        if len(cur) + len(block) > TG_LIMIT and cur.strip() != header.strip():
            messages.append(cur.rstrip())
            cur = header + block
        else:
            cur += block
    if cur.strip():
        messages.append(cur.rstrip())
    return messages


def flush_digest(cfg):
    """Шлёт накопленный дайджест и очищает очередь (команда --flush)."""
    pending = load_json(PENDING_PATH, [])
    if not pending:
        tg_send("🗂 За прошедший период новых подходящих конкурсов не появилось.")
        print("Очередь пуста — дайджест не отправлен.")
        return
    for msg in build_digest(pending):
        tg_send(msg)
        time.sleep(0.4)
    PENDING_PATH.write_text("[]\n", encoding="utf-8")
    print(f"✅ Дайджест отправлен: {len(pending)} позиций, очередь очищена.")


def chunk_messages(lines, limit=TG_LIMIT):
    """Склеивает строки в сообщения, не превышающие лимит Telegram."""
    messages = []
    cur = ""
    for ln in lines:
        piece = ("\n" if cur else "") + ln
        if cur and len(cur) + len(piece) > limit:
            messages.append(cur)
            cur = ln
        else:
            cur += piece
    if cur:
        messages.append(cur)
    return messages


# ─────────────────────────  Архив  ─────────────────────────

def archive_upsert(archive, src, it, cfg, today):
    """Добавляет/обновляет запись в историческом архиве (по ключу)."""
    kws = matched_keywords(it["text"], cfg)
    rec = archive.get(it["key"])
    if rec:
        rec["title"] = it["title"]
        rec["link"] = it["link"]
        rec["source"] = src["name"]
        rec["emoji"] = src.get("emoji", "📌")
        rec["keywords"] = sorted(set(rec.get("keywords", [])) | set(kws))
        rec["last_seen"] = today
    else:
        archive[it["key"]] = {
            "title": it["title"],
            "link": it["link"],
            "source": src["name"],
            "emoji": src.get("emoji", "📌"),
            "keywords": kws,
            "first_seen": today,
            "last_seen": today,
        }


def search_archive(archive, query, cfg):
    """Ищет по архиву: заголовок, источник и направления (ключевые слова).

    Гибкое совпадение — запрос как подстрока заголовка/источника, а по
    направлениям срабатывает и «горнодобыча» → корень «горн», и наоборот.
    """
    q = norm(query)
    if not q:
        return []
    tokens = q.split()
    results = []
    for rec in archive.values():
        hay = norm(rec.get("title", "") + " " + rec.get("source", ""))
        kws = [k.lower() for k in rec.get("keywords", [])]
        hit = q in hay or all(
            (t in hay) or any(t in kw or kw in t for kw in kws) for t in tokens
        )
        if hit:
            results.append(rec)
    results.sort(key=lambda r: r.get("first_seen", ""), reverse=True)
    return results


# ─────────────────────  Отчёт (--report)  ─────────────────────

def build_report(archive, cfg):
    """Markdown-отчёт, сгруппированный по направлениям (для Ctrl+F)."""
    by_kw = {}
    for rec in archive.values():
        for kw in rec.get("keywords", []) or ["(без направления)"]:
            by_kw.setdefault(kw, []).append(rec)

    order = sorted(by_kw, key=lambda k: (-len(by_kw[k]), k))
    out = [
        "# 🗂 Архив грантов (грант-радар)",
        "",
        f"Всего записей: **{len(archive)}**. Обновлено: {today_iso()}.",
        "",
        "## Направления",
        "",
    ]
    for kw in order:
        anchor = re.sub(r"[^\w]+", "-", kw.strip()).strip("-").lower()
        out.append(f"- [{kw}](#{anchor}) — {len(by_kw[kw])}")
    out.append("")

    for kw in order:
        out.append(f"## {kw}")
        out.append("")
        recs = sorted(by_kw[kw], key=lambda r: r.get("first_seen", ""), reverse=True)
        for r in recs:
            date = r.get("first_seen", "?")
            out.append(f"- {date} — [{r['title']}]({r['link']}) · {r.get('source','')}")
        out.append("")
    return "\n".join(out)


def cmd_report(cfg):
    archive = load_json(ARCHIVE_PATH, {})
    if not archive:
        print("⚠️  Архив пуст — сначала запусти обход (python radar.py).")
        return
    REPORT_PATH.write_text(build_report(archive, cfg), encoding="utf-8")
    print(f"📄 Отчёт записан: {REPORT_PATH.name} ({len(archive)} записей)")


# ────────────────  Поиск: локально и через бот  ────────────────

def format_results(query, results, cfg):
    """Список найденных грантов в виде готовых Telegram-сообщений."""
    if not results:
        return [
            f"🔍 По запросу «{esc(query)}» в архиве пока пусто.\n"
            "Архив копится с момента запуска — попробуй позже или другой запрос."
        ]
    limit = cfg.get("bot_max_results", 20)
    shown = results[:limit]
    head = f"🔍 <b>{esc(query)}</b> — найдено {len(results)}"
    if len(results) > limit:
        head += f", показываю первые {limit}"
    lines = [head, ""]
    for r in shown:
        lines.append(
            f"{r.get('emoji', '📌')} <a href=\"{esc(r['link'])}\">{esc(r['title'])}</a>"
        )
        meta = " · ".join(x for x in [r.get("first_seen", ""), r.get("source", "")] if x)
        if meta:
            lines.append(f"<i>{esc(meta)}</i>")
    return chunk_messages(lines)


def bot_help(cfg):
    return (
        "🛰 <b>Грант-радар: поиск по архиву</b>\n"
        "Пришли тему — покажу, какие гранты по ней уже встречались.\n\n"
        "Примеры: <code>горн</code>, <code>малое предприятие</code>, "
        "<code>искусственный интеллект</code>\n"
        "Или командой: <code>/grants биотех</code>\n\n"
        "👇 Либо выбери направление кнопкой ниже."
    )


def directions_keyboard(cfg, archive):
    """Inline-клавиатура с направлениями (по 2 кнопки в ряд).

    Берём список из config["directions"]; если его нет — топ направлений
    из самого архива по количеству находок.
    """
    dirs = cfg.get("directions")
    if not dirs:
        counts = {}
        for r in archive.values():
            for k in r.get("keywords", []):
                counts[k] = counts.get(k, 0) + 1
        top = sorted(counts, key=lambda k: (-counts[k], k))[:12]
        dirs = [{"label": k, "query": k} for k in top]
    if not dirs:
        return None

    rows, row = [], []
    for d in dirs:
        row.append({"text": d["label"], "callback_data": "d:" + d["query"]})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def handle_query(text, archive, cfg):
    """Разбирает сообщение/нажатие и возвращает список {text, markup}."""
    q = (text or "").strip()
    if q.startswith("/"):
        parts = q.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]  # убираем @имя_бота в группах
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/start", "/help", "/directions", "/napravleniya"):
            arg = ""  # покажем подсказку с кнопками
        q = arg  # /grants, /search и пр. — берём остаток как запрос

    texts = [bot_help(cfg)] if not q else format_results(
        q, search_archive(archive, q, cfg), cfg
    )
    out = [{"text": t, "markup": None} for t in texts]
    # Клавиатуру направлений цепляем к последнему сообщению — всегда под рукой.
    kb = directions_keyboard(cfg, archive)
    if out and kb:
        out[-1]["markup"] = kb
    return out


def cmd_search(cfg, query):
    """Локальный поиск: печатает результаты в консоль."""
    archive = load_json(ARCHIVE_PATH, {})
    for msg in format_results(query, search_archive(archive, query, cfg), cfg):
        # убираем HTML-теги для читаемого вывода в терминал
        print(re.sub(r"<[^>]+>", "", msg))
        print()


def tg_get_updates(offset, timeout):
    r = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"offset": offset, "timeout": timeout},
        timeout=timeout + 10,
    )
    r.raise_for_status()
    return r.json().get("result", [])


def bot_poll(cfg):
    """Один цикл опроса Telegram (команда --poll, дёргается воркфлоу bot.yml)."""
    if not TOKEN:
        sys.exit("❌ Нет TELEGRAM_BOT_TOKEN — бот не может опрашивать Telegram.")

    archive = load_json(ARCHIVE_PATH, {})
    state = load_json(BOT_STATE_PATH, {})
    offset = state.get("offset", 0)

    window = cfg.get("bot_poll_seconds", 40)
    long_poll = min(20, window)
    deadline = time.time() + window
    handled = 0

    while time.time() < deadline:
        try:
            updates = tg_get_updates(offset, timeout=long_poll)
        except Exception as e:
            print(f"⚠️  getUpdates упал: {e}")
            break
        for up in updates:
            offset = up["update_id"] + 1

            cq = up.get("callback_query")
            if cq:
                data = cq.get("data", "")
                query = data[2:] if data.startswith("d:") else data
                chat_id = cq["message"]["chat"]["id"]
                for m in handle_query(query, archive, cfg):
                    tg_send(m["text"], chat_id=chat_id, reply_markup=m["markup"])
                    time.sleep(0.3)
                tg_answer_callback(cq["id"])
                handled += 1
                continue

            msg = up.get("message") or up.get("edited_message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            for m in handle_query(msg.get("text", ""), archive, cfg):
                tg_send(m["text"], chat_id=chat_id, reply_markup=m["markup"])
                time.sleep(0.3)
            handled += 1
        state["offset"] = offset
        BOT_STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )

    print(f"🤖 Обработано сообщений: {handled}, offset={offset}")


def main():
    args = sys.argv[1:]

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("❌ Нет config.json")

    if "--flush" in args:
        flush_digest(cfg)
        return
    if "--poll" in args or "--bot" in args:
        bot_poll(cfg)
        return
    if "--report" in args:
        cmd_report(cfg)
        return
    if "--search" in args:
        i = args.index("--search")
        query = " ".join(args[i + 1:]).strip()
        if not query:
            sys.exit("Использование: python radar.py --search <тема>")
        cmd_search(cfg, query)
        return

    mode = str(cfg.get("mode", "stream")).lower()

    seen = load_json(SEEN_PATH, None)
    first_run = seen is None
    seen = seen or {}

    archive = load_json(ARCHIVE_PATH, {})
    today = today_iso()

    found = scan(cfg)
    new_items = [(s, it) for (s, it) in found if it["key"] not in seen]

    # Запоминаем всё найденное сразу (даже если не отправим/отложим)
    # и одновременно копим исторический архив для поиска.
    for src, it in found:
        seen[it["key"]] = it["title"]
        archive_upsert(archive, src, it, cfg, today)

    if first_run:
        extra = (
            " Коплю их и пришлю одним дайджестом по расписанию."
            if mode == "digest"
            else " Новые уведомления пойдут со следующих проверок."
        )
        tg_send(
            "🛰 <b>Грант-радар запущен!</b>\n"
            f"Слежу за источниками ({len(cfg['sources'])} шт.) в режиме "
            f"<b>{esc(mode)}</b>.\n"
            f"Сейчас в поле зрения — {len(found)} актуальных позиций."
            + extra
        )
    elif mode == "digest":
        # Копим новые находки в очередь — отправит отдельный запуск --flush.
        pending = load_json(PENDING_PATH, [])
        have = {p["key"] for p in pending}
        added = 0
        for src, it in new_items:
            if it["key"] in have:
                continue
            have.add(it["key"])
            pending.append(
                {
                    "key": it["key"],
                    "title": it["title"],
                    "link": it["link"],
                    "name": src["name"],
                    "emoji": src.get("emoji", "📌"),
                }
            )
            added += 1
        PENDING_PATH.write_text(
            json.dumps(pending, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"🗂 Отложено в дайджест: +{added}, всего в очереди {len(pending)}")
    else:
        cap = cfg.get("max_alerts_per_run", 25)
        to_send = new_items[:cap]
        for src, it in to_send:
            tg_send(stream_item_msg(src, it))
            time.sleep(0.4)
        if len(new_items) > cap:
            tg_send(f"…и ещё {len(new_items) - cap} новых — покажу в следующий раз.")
        print(f"✅ Новых: {len(new_items)}, отправлено: {len(to_send)}")

    SEEN_PATH.write_text(
        json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    ARCHIVE_PATH.write_text(
        json.dumps(archive, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"💾 seen.json: {len(seen)} записей · archive.json: {len(archive)}")


if __name__ == "__main__":
    main()
