#!/usr/bin/env python3
"""Собирает данные клана и пишет data/db.json для сайта.

Запускается каждые 5 минут из GitHub Actions. Ключ API берётся из COC_API_TOKEN,
запросы идут через прокси RoyaleAPI (COC_API_BASE), поэтому у ключа один
постоянный IP: 45.79.218.79.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COC_DATA_DIR", str(ROOT / "state"))
os.environ.setdefault("COC_API_BASE", "https://cocproxy.royaleapi.dev/v1")
os.environ.setdefault("COC_CLAN_TAG", "#8R92VY9U")
os.environ.setdefault("TZ", "Europe/Zurich")
try:
    time.tzset()
except AttributeError:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parent))
import coc_clan as coc  # noqa: E402

DB = ROOT / "data" / "db.json"
TZ = ZoneInfo("Europe/Zurich")


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def kind_of(md: str) -> str:
    if md.startswith("⚠️") or "Ошибка API" in md:
        return "err"
    return "ok" if "|" in md else "info"


def safe(fn, *a):
    try:
        return fn(*a)
    except coc.CocError as e:
        return f"⚠️ {e}"
    except Exception as e:  # noqa: BLE001
        return f"⚠️ Ошибка API: {e}"


def main() -> int:
    db = json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else {}
    for c in ("snapshots", "history", "comments", "meta", "players"):
        db.setdefault(c, {})

    now = datetime.now(TZ)
    at = int(time.time() * 1000)
    week = now.strftime("%G-W%V")

    tools = {
        "overview": (coc.clan_overview,),
        "activity": (coc.member_activity, 7),
        "war": (coc.current_war_status,),
        "cwl": (coc.cwl_status,),
        "raids": (coc.raid_weekend,),
        "log": (coc.war_log, 10),
        "missed": (coc.missed_attacks_history, 10),
    }
    res = {k: safe(*v) for k, v in tools.items()}
    kinds = {k: kind_of(v) for k, v in res.items()}
    if all(k == "err" for k in kinds.values()):
        print("Все запросы вернули ошибку:", res["overview"], file=sys.stderr)
        db["meta"]["status"] = {"ok": False, "error": res["overview"], "at": at}
        DB.write_text(json.dumps(db, ensure_ascii=False, indent=0), encoding="utf-8")
        return 1

    # 1. снимки
    for k, md in res.items():
        if kinds[k] != "err":
            db["snapshots"][k] = {"md": md, "at": at, "kind": kinds[k]}

    # 2. история
    hist = db["history"]

    def put(doc_id: str, md: str, extra: dict | None = None) -> None:
        cur = hist.get(doc_id)
        if not cur:
            hist[doc_id] = {"md": md, "at": at, "first_at": at, **(extra or {})}
        elif cur.get("md") != md:
            cur.update({"md": md, "at": at, **(extra or {})})

    if kinds["overview"] == "ok":
        put("don-" + now.strftime("%Y%m%d"), res["overview"])
    war = res["war"]
    if kinds["war"] == "ok" and re.search("День боя|Война завершена", war):
        first = war.split("\n")[0]
        h = md5(f"{first} {week}")
        put("war-" + h, war)
        # снимок на отметке 12 часов боя (бой идёт 24 часа)
        m = re.search(r"До конца войны:\s*(?:(\d+)\s*ч)?\s*(?:(\d+)\s*мин)?", war)
        if "День боя" in war and m:
            left = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
            w12 = hist.get("war12-" + h)
            if left >= 12 * 60:
                put("war12-" + h, war, {"final12": False})
            elif w12 and not w12.get("final12"):
                w12["final12"] = True
    raids = res["raids"]
    if kinds["raids"] == "ok" and re.search(r"\((завершены|идут)\)", raids.split("\n")[0]):
        # ключ по датам выходных: идущие рейды и итог — один документ
        key = raids.split("\n")[0].replace("(идут)", "(завершены)")
        put("raid-" + md5(key), raids)
    cwl = res["cwl"]
    if kinds["cwl"] == "ok" and "|" in cwl:
        put("cwl-" + md5(cwl.split("\n")[0]), cwl)

    # 3. звания: кого повысили/понизили — не трогаем 7 дней
    if kinds["overview"] == "ok":
        roles = {}
        for line in res["overview"].split("\n"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 4 and cells[0].isdigit():
                roles[cells[1]] = cells[3]
        meta = db["meta"].setdefault("roles", {"baseline": {}, "changes": {}, "left": {}})
        base = meta.setdefault("baseline", {})
        changes = meta.setdefault("changes", {})
        today = now.strftime("%Y-%m-%d")
        until = (now + timedelta(days=7)).strftime("%Y-%m-%d")
        for name, role in roles.items():
            old = base.get(name)
            if old and old != role:
                changes[name] = {"from": old, "to": role, "date": today, "until": until}
            base[name] = role
        for name in list(base):
            if name not in roles:
                meta.setdefault("left", {})[name] = today
                base.pop(name)

    # 4. армия игроков — раз в 6 часов (и при первом запуске)
    last = db["meta"].get("players_at", 0)
    if at - last > 6 * 3600 * 1000 or not db["players"]:
        try:
            clan = coc._get_clan()
            fresh = {}
            for m in clan.get("memberList", []):
                try:
                    md = coc._player_md(coc.api_get(f"/players/{coc._enc(m['tag'])}"))
                    fresh[m["tag"].lstrip("#").upper()] = {"md": md, "at": at}
                except Exception as e:  # noqa: BLE001
                    print("игрок", m.get("name"), e, file=sys.stderr)
            if fresh:
                db["players"] = fresh
                db["meta"]["players_at"] = at
        except Exception as e:  # noqa: BLE001
            print("армия:", e, file=sys.stderr)

    db["meta"]["status"] = {"ok": True, "at": at}
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.write_text(json.dumps(db, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print("ok", {k: v for k, v in kinds.items()}, "players:", len(db["players"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
