#!/usr/bin/env python3
"""Автопубликация в Instagram (@vkusnyashki_uk) через официальный Graph API.

Очередь лежит в queue.json: у каждого поста есть время (по Алматы), файл, подпись и статус.
  pending      — ждёт своего времени
  needs_photo  — нет фото товара, пропускается, пока не выполнить `unlock`
  publishing   — публикуется прямо сейчас (если «завис» — проверьте вручную)
  published    — опубликован (сохранён media_id)
  failed       — ошибка (текст в поле error), можно повторить командой `retry`
  missed       — время прошло больше чем на 12 часов, не опубликован

Команды:
  python3 publish.py check            проверить подключение (ник и количество публикаций)
  python3 publish.py list             показать очередь
  python3 publish.py run              опубликовать всё, что уже пора (запускать каждые 15 минут)
  python3 publish.py run --dry-run    показать, что было бы опубликовано
  python3 publish.py now ID           опубликовать пост из очереди прямо сейчас
  python3 publish.py unlock ID        снять пометку needs_photo (после замены картинки)
  python3 publish.py retry ID         вернуть failed/missed обратно в pending (время станет «сейчас»)
  python3 publish.py add FILE --at "2026-11-10 12:00" [--caption "..."] [--reel]
Если в папке есть файл PAUSED — `run` ничего не публикует (пауза).

Настройки (переменные окружения или файл .env):
  IG_USER_ID, IG_ACCESS_TOKEN  — обязательно
  PUBLIC_BASE_URL              — адрес, по которому файлы из репозитория доступны публично,
                                 например https://raw.githubusercontent.com/USER/REPO/main
                                 (или IMGBB_API_KEY — только для картинок, если нет публичного адреса)
  GRAPH_HOST                   — по умолчанию https://graph.facebook.com (для входа через Instagram Login:
                                 https://graph.instagram.com)
  GRAPH_VERSION                — по умолчанию v21.0
"""
import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Almaty")
except Exception:  # на случай системы без базы часовых поясов: Алматы = UTC+5
    TZ = timezone(timedelta(hours=5))

HERE = os.path.dirname(os.path.abspath(__file__))
QUEUE = os.path.join(HERE, "queue.json")
MAX_PER_RUN = 3          # не больше трёх публикаций за один запуск
MISSED_AFTER_H = 12      # просроченные больше чем на 12 часов не публикуем автоматически
STUCK_AFTER_MIN = 30     # «publishing» дольше 30 минут считаем зависшим


def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
IG_USER_ID = os.environ.get("IG_USER_ID", "")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
IMGBB_KEY = os.environ.get("IMGBB_API_KEY", "")
GRAPH = f"{os.environ.get('GRAPH_HOST', 'https://graph.facebook.com').rstrip('/')}/{os.environ.get('GRAPH_VERSION', 'v21.0')}"


def now():
    return datetime.now(TZ)


def parse_when(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def need_config():
    missing = [k for k, v in (("IG_USER_ID", IG_USER_ID), ("IG_ACCESS_TOKEN", TOKEN)) if not v]
    if missing:
        sys.exit("Не заданы настройки: " + ", ".join(missing) + " (см. .env.example)")


def load_queue():
    return json.load(open(QUEUE, encoding="utf-8")) if os.path.exists(QUEUE) else []


def save_queue(q):
    tmp = QUEUE + ".tmp"
    json.dump(q, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, QUEUE)


def api(method, path, **params):
    params["access_token"] = TOKEN
    r = requests.request(method, f"{GRAPH}/{path}", params=params if method == "GET" else None,
                         data=params if method == "POST" else None, timeout=120)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Graph API: ответ не JSON (HTTP {r.status_code})")
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", data)
        if isinstance(err, dict) and err.get("code") == 190:
            raise RuntimeError("Токен недействителен или истёк (код 190): создайте новый IG_ACCESS_TOKEN")
        raise RuntimeError(f"Graph API: {err}")
    return data


def public_url(media_path, is_video):
    """Graph API принимает только публичную ссылку на файл."""
    if BASE_URL:
        return BASE_URL + "/" + quote(media_path.replace(os.sep, "/"))
    if is_video:
        sys.exit("Для Reels нужен PUBLIC_BASE_URL (публичный адрес с файлами).")
    if not IMGBB_KEY:
        sys.exit("Нужен PUBLIC_BASE_URL или IMGBB_API_KEY в настройках.")
    with open(os.path.join(HERE, media_path), "rb") as f:
        b64 = base64.b64encode(f.read())
    r = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_KEY, "image": b64}, timeout=120)
    r.raise_for_status()
    return r.json()["data"]["url"]


def wait_ready(container_id, tries=60, delay=5):
    for _ in range(tries):
        st = api("GET", container_id, fields="status_code")["status_code"]
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram не принял файл: статус {st}")
        time.sleep(delay)
    raise RuntimeError("Таймаут обработки файла в Instagram")


def publish_item(it):
    need_config()
    is_reel = it["type"] == "reel"
    url = public_url(it["media"], is_reel)
    if is_reel:
        c = api("POST", f"{IG_USER_ID}/media", media_type="REELS", video_url=url, caption=it["caption"],
                share_to_feed="true")["id"]
    else:
        c = api("POST", f"{IG_USER_ID}/media", image_url=url, caption=it["caption"])["id"]
    wait_ready(c)
    return api("POST", f"{IG_USER_ID}/media_publish", creation_id=c)["id"]


def find(q, ident):
    for it in q:
        if it["id"] == ident or it["id"].split("_", 1)[-1] == ident:
            return it
    sys.exit(f"В очереди нет поста «{ident}»")


def cmd_check(_):
    need_config()
    d = api("GET", IG_USER_ID, fields="username,media_count")
    print("Подключено:", d.get("username"), "· публикаций:", d.get("media_count"))
    print("Файлы берутся с:", BASE_URL or "imgbb (только картинки)")


def cmd_list(_):
    for it in load_queue():
        extra = f"  ({it['error']})" if it.get("error") else ""
        print(f"{it['id']:<28} {it['publish_at']}  [{it['status']}] {it['type']}{extra}")


def cmd_run(a):
    if os.path.exists(os.path.join(HERE, "PAUSED")):
        print("Пауза (файл PAUSED), ничего не публикуем.")
        return
    q = load_queue()
    t = now()
    # зависшие публикации не повторяем автоматически, чтобы не было дублей
    for it in q:
        if it["status"] == "publishing" and t - datetime.fromisoformat(it["started"]) > timedelta(minutes=STUCK_AFTER_MIN):
            it["status"], it["error"] = "failed", "Публикация зависла: проверьте ленту Instagram и, если поста там нет, выполните retry"
    done = 0
    for it in q:
        if it["status"] != "pending":
            continue
        due = parse_when(it["publish_at"])
        if due > t:
            continue
        if t - due > timedelta(hours=MISSED_AFTER_H) and not a.catch_up:
            it["status"] = "missed"
            continue
        if done >= MAX_PER_RUN:
            break
        print(("[dry-run] " if a.dry_run else "") + f"Публикую {it['id']} (по плану {it['publish_at']})")
        if a.dry_run:
            continue
        it["status"], it["started"] = "publishing", t.isoformat()
        save_queue(q)
        try:
            it["media_id"] = publish_item(it)
            it["status"] = "published"
            it["published_at"] = now().isoformat(timespec="minutes")
            it.pop("error", None)
            done += 1
        except Exception as e:  # ошибка одного поста не останавливает остальные
            it["status"], it["error"] = "failed", str(e)[:300]
            print("  ошибка:", e)
        save_queue(q)
    if not a.dry_run:
        save_queue(q)
    print(f"Готово, опубликовано: {done}")


def cmd_now(a):
    q = load_queue()
    it = find(q, a.id)
    it["status"], it["started"] = "publishing", now().isoformat()
    save_queue(q)
    try:
        it["media_id"] = publish_item(it)
        it["status"], it["published_at"] = "published", now().isoformat(timespec="minutes")
        it.pop("error", None)
        print("Опубликовано, media_id:", it["media_id"])
    except Exception as e:
        it["status"], it["error"] = "failed", str(e)[:300]
        print("Ошибка:", e)
    save_queue(q)


def cmd_unlock(a):
    q = load_queue()
    it = find(q, a.id)
    if it["status"] != "needs_photo":
        sys.exit(f"Статус поста: {it['status']}, снимать нечего")
    it["status"] = "pending"
    save_queue(q)
    print("Готово: пост снова в очереди")


def cmd_retry(a):
    q = load_queue()
    it = find(q, a.id)
    if it["status"] not in ("failed", "missed"):
        sys.exit(f"Статус поста: {it['status']}, повторять не нужно")
    it["status"] = "pending"
    it["publish_at"] = now().strftime("%Y-%m-%d %H:%M")
    it.pop("error", None)
    save_queue(q)
    print("Готово: пост будет опубликован при ближайшем запуске")


def cmd_add(a):
    q = load_queue()
    cap = a.caption
    if not cap:
        txt = os.path.splitext(a.file)[0] + ".txt"
        if not os.path.exists(txt):
            sys.exit("Укажите --caption или положите рядом файл с подписью (.txt)")
        cap = open(txt, encoding="utf-8").read().strip()
    ident = f"{len(q) + 1:02d}_{os.path.splitext(os.path.basename(a.file))[0]}"
    q.append({"id": ident, "type": "reel" if a.reel else "image", "media": a.file, "caption": cap,
              "publish_at": a.at, "status": "pending"})
    q.sort(key=lambda x: x["publish_at"])
    save_queue(q)
    print("Добавлено:", ident, a.at)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check").set_defaults(f=cmd_check)
    sp.add_parser("list").set_defaults(f=cmd_list)
    p = sp.add_parser("run"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--catch-up", action="store_true"); p.set_defaults(f=cmd_run)
    for name, fn in (("now", cmd_now), ("unlock", cmd_unlock), ("retry", cmd_retry)):
        p = sp.add_parser(name); p.add_argument("id"); p.set_defaults(f=fn)
    p = sp.add_parser("add"); p.add_argument("file"); p.add_argument("--at", required=True)
    p.add_argument("--caption", default=""); p.add_argument("--reel", action="store_true"); p.set_defaults(f=cmd_add)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
