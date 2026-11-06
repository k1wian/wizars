#!/usr/bin/env python3
"""
Clash of Clans — помощник по управлению кланом для Claude (MCP-сервер).

Использует только официальный API Supercell (только чтение) — ничего не делает
в игре от вашего имени, поэтому правила игры не нарушает.

Режимы запуска:
  python coc_clan.py           — MCP-сервер (для Claude Desktop / Claude Code)
  python coc_clan.py check     — проверить ключ и тег клана
  python coc_clan.py report    — напечатать сводный отчёт по клану (Markdown)

Переменные окружения:
  COC_API_TOKEN  — ключ с developer.clashofclans.com (обязательно)
  COC_PLAYER_TAG — ваш тег игрока; клан определяется по нему автоматически
  COC_CLAN_TAG   — или тег клана напрямую (если задан, имеет приоритет)
  COC_DATA_DIR   — папка для истории активности и войн (по умолчанию ~/.coc_clan)
"""
from __future__ import annotations

import functools
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    # Доверять тем же сертификатам, что и система (Связка ключей macOS / хранилище Windows).
    # Нужно, если HTTPS проверяют антивирус, AdGuard, VPN или сеть организации.
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

API = os.environ.get("COC_API_BASE", "https://api.clashofclans.com/v1").rstrip("/")

ROLES = {
    "leader": "Глава",
    "coLeader": "Соруководитель",
    "admin": "Старейшина",
    "member": "Участник",
}
WAR_STATES = {
    "preparation": "День подготовки",
    "inWar": "День боя",
    "warEnded": "Война завершена",
}
WAR_RESULTS = {"win": "Победа", "lose": "Поражение", "tie": "Ничья"}

# Поля, по изменению которых мы понимаем, что игрок заходил в игру.
TRACKED_FIELDS = ("donations", "donationsReceived", "trophies", "builderBaseTrophies", "expLevel")


# --------------------------------------------------------------------------- #
# Базовые функции: API, время, хранилище
# --------------------------------------------------------------------------- #

class CocError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def _token() -> str:
    token = os.environ.get("COC_API_TOKEN", "").strip()
    if not token:
        raise CocError("Не задан COC_API_TOKEN — ключ с developer.clashofclans.com.")
    return token


def _normalize_tag(tag: str) -> str:
    tag = tag.strip().upper().replace("O", "0")  # в тегах нет буквы O, только ноль
    if not tag:
        raise CocError("Пустой тег.")
    return tag if tag.startswith("#") else "#" + tag


_CLAN_TAG_CACHE: str | None = None


def _clan_tag() -> str:
    """Тег клана из COC_CLAN_TAG, а если не задан — клан игрока из COC_PLAYER_TAG."""
    global _CLAN_TAG_CACHE
    tag = os.environ.get("COC_CLAN_TAG", "")
    if tag.strip():
        return _normalize_tag(tag)
    if _CLAN_TAG_CACHE:
        return _CLAN_TAG_CACHE
    player = os.environ.get("COC_PLAYER_TAG", "")
    if not player.strip():
        raise CocError("Задайте COC_PLAYER_TAG (ваш тег игрока) или COC_CLAN_TAG (тег клана).")
    p = api_get(f"/players/{_enc(_normalize_tag(player))}")
    if not p.get("clan"):
        raise CocError(f"Игрок {p.get('name')} сейчас не состоит в клане.")
    _CLAN_TAG_CACHE = p["clan"]["tag"]
    return _CLAN_TAG_CACHE


def _enc(tag: str) -> str:
    return urllib.parse.quote(tag, safe="")


def api_get(path: str) -> dict:
    req = urllib.request.Request(
        API + path,
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            reason = json.loads(body).get("reason", "")
        except json.JSONDecodeError:
            reason = ""
        messages = {
            400: "Неверный запрос.",
            403: ("Доступ запрещён. Проверьте ключ и что в его настройках на "
                  "developer.clashofclans.com указан текущий IP-адрес этого компьютера."
                  if reason == "accessDenied.invalidIp" or "invalidIp" in body
                  else "Доступ запрещён (ключ неверный или данные закрыты, например журнал войн)."),
            404: "Не найдено — проверьте тег.",
            429: "Слишком много запросов, попробуйте через минуту.",
            503: "Сервера игры на техобслуживании.",
        }
        raise CocError(f"Ошибка API {e.code}: {messages.get(e.code, reason or body[:200])}", e.code)
    except urllib.error.URLError as e:
        raise CocError(f"Нет связи с API: {e.reason}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


def _fmt_left(dt: datetime) -> str:
    seconds = int((dt - _now()).total_seconds())
    if seconds <= 0:
        return "время вышло"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} ч {minutes} мин" if hours else f"{minutes} мин"


def _data_dir() -> Path:
    d = Path(os.environ.get("COC_DATA_DIR") or Path.home() / ".coc_clan")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load(name: str, default):
    try:
        return json.loads((_data_dir() / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(name: str, data) -> None:
    path = _data_dir() / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _key(tag: str) -> str:
    return tag.lstrip("#")


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return "_(пусто)_"
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in row) + " |")
    return "\n".join(out)


def _get_clan() -> dict:
    return api_get(f"/clans/{_enc(_clan_tag())}")


# --------------------------------------------------------------------------- #
# История активности и войн (API не отдаёт «последний вход», поэтому
# сохраняем снимки и смотрим, когда у игрока последний раз что-то менялось)
# --------------------------------------------------------------------------- #

def _update_activity(clan: dict) -> dict:
    fname = f"activity_{_key(clan['tag'])}.json"
    store = _load(fname, {"members": {}})
    now = _now().isoformat()
    current = set()
    for m in clan.get("memberList", []):
        tag = m["tag"]
        current.add(tag)
        values = {f: m.get(f) for f in TRACKED_FIELDS}
        rec = store["members"].get(tag)
        if rec is None or rec.get("left_at"):
            store["members"][tag] = {"name": m["name"], "values": values,
                                     "first_seen": now, "last_change": now}
        else:
            if rec["values"] != values:
                rec["values"] = values
                rec["last_change"] = now
            rec["name"] = m["name"]
    for tag, rec in store["members"].items():
        if tag not in current and not rec.get("left_at"):
            rec["left_at"] = now
    store["updated"] = now
    _save(fname, store)
    return store["members"]


def _record_war(war: dict, clan_tag: str, kind: str, attacks_per_member: int) -> None:
    """Сохраняет итоги войны по участникам — для истории пропущенных атак."""
    if war.get("state") not in ("inWar", "warEnded"):
        return
    if war["clan"]["tag"] == clan_tag:
        ours, opp = war["clan"], war["opponent"]
    elif war["opponent"]["tag"] == clan_tag:
        ours, opp = war["opponent"], war["clan"]
    else:
        return
    fname = f"wars_{_key(clan_tag)}.json"
    store = _load(fname, {})
    key = f"{war.get('preparationStartTime', war.get('startTime'))}_{opp['tag']}"
    store[key] = {
        "kind": kind,
        "opponent": opp.get("name"),
        "end": war.get("endTime"),
        "state": war["state"],
        "attacks_per_member": attacks_per_member,
        "members": {
            m["tag"]: {"name": m["name"], "used": len(m.get("attacks", [])),
                       "stars": sum(a["stars"] for a in m.get("attacks", []))}
            for m in ours.get("members", [])
        },
    }
    _save(fname, store)


# --------------------------------------------------------------------------- #
# Инструменты для Claude
# --------------------------------------------------------------------------- #

TOOLS = []


def tool(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except CocError as e:
            return f"⚠️ {e}"
    TOOLS.append(wrapper)
    return wrapper


@tool
def clan_overview() -> str:
    """Общая информация о клане и список участников: роли, ратуша, кубки, донаты."""
    clan = _get_clan()
    _update_activity(clan)
    lines = [
        f"## {clan['name']} ({clan['tag']}), уровень {clan.get('clanLevel')}",
        f"Участников: {clan.get('members')}/50 · Лига КВ: {clan.get('warLeague', {}).get('name', '—')} · "
        f"Лига столицы: {clan.get('capitalLeague', {}).get('name', '—')}",
        f"Войны: побед {clan.get('warWins', 0)}, поражений {clan.get('warLosses', '?')}, "
        f"ничьих {clan.get('warTies', '?')} · серия побед: {clan.get('warWinStreak', 0)} · "
        f"журнал войн {'открыт' if clan.get('isWarLogPublic') else 'закрыт'}",
        f"Требования для вступления: {clan.get('requiredTrophies', 0)} кубков, "
        f"ТХ {clan.get('requiredTownhallLevel', 1)}+ · тип: {clan.get('type')}",
        "",
    ]
    members = sorted(clan.get("memberList", []), key=lambda m: m.get("clanRank", 99))
    rows = [[m.get("clanRank"), m["name"], m.get("tag", "—"), ROLES.get(m.get("role"), m.get("role")),
             m.get("townHallLevel", "?"), m.get("expLevel"), m.get("trophies"),
             m.get("donations", 0), m.get("donationsReceived", 0),
             ((m.get("leagueTier") or {}).get("name") or (m.get("league") or {}).get("name") or "—")] for m in members]
    lines.append(_table(["#", "Игрок", "Тег", "Роль", "ТХ", "Ур.", "Кубки", "Отдал", "Получил", "Лига"], rows))
    return "\n".join(lines)


@tool
def member_activity(inactive_days: int = 7) -> str:
    """Активность участников: донаты, соотношение отдал/получил и сколько дней
    у игрока ничего не менялось (признак неактивности). inactive_days — порог в днях.
    История копится с первого запуска, поэтому чем чаще вызывать, тем точнее."""
    clan = _get_clan()
    activity = _update_activity(clan)
    now = _now()
    rows, inactive, no_donations = [], [], []
    max_observed = 0
    for m in sorted(clan.get("memberList", []), key=lambda m: m.get("clanRank", 99)):
        rec = activity[m["tag"]]
        since = (now - datetime.fromisoformat(rec["last_change"])).days
        observed = (now - datetime.fromisoformat(rec["first_seen"])).days
        max_observed = max(max_observed, observed)
        given, got = m.get("donations", 0), m.get("donationsReceived", 0)
        ratio = f"{given / got:.1f}" if got else ("∞" if given else "—")
        seen = (f"изменений нет {observed} дн. (с начала наблюдения)"
                if rec["last_change"] == rec["first_seen"] else
                ("сегодня" if since == 0 else f"{since} дн. назад"))
        rows.append([m["name"], ROLES.get(m.get("role"), m.get("role")), m.get("townHallLevel", "?"),
                     given, got, ratio, seen])
        if since >= inactive_days:
            inactive.append(f"{m['name']} ({ROLES.get(m.get('role'), '')}) — {since} дн.")
        if given == 0:
            no_donations.append(m["name"])

    lines = ["## Активность участников",
             _table(["Игрок", "Роль", "ТХ", "Отдал", "Получил", "Отд/Пол", "Последняя активность"], rows), ""]
    if max_observed < inactive_days:
        lines.append(f"ℹ️ Наблюдение ведётся всего {max_observed} дн. — для порога {inactive_days} дн. "
                     "истории пока мало. Вызывайте этот инструмент хотя бы раз в день.")
    lines.append(f"**Неактивны {inactive_days}+ дн.:** " + (", ".join(inactive) if inactive else "нет"))
    lines.append("**Ни одного доната за сезон:** " + (", ".join(no_donations) if no_donations else "нет")
                 + " (донаты обнуляются в начале каждого сезона)")
    return "\n".join(lines)


@tool
def current_war_status() -> str:
    """Текущая клановая война: счёт, время до конца, атаки каждого участника
    и кто ещё не использовал атаки."""
    tag = _clan_tag()
    try:
        war = api_get(f"/clans/{_enc(tag)}/currentwar")
    except CocError as e:
        if e.code == 403:
            return "Журнал войн клана закрыт — откройте его в настройках клана, чтобы видеть войны."
        raise
    state = war.get("state")
    if state in (None, "notInWar"):
        return "Клан сейчас не участвует в клановой войне (во время ЛВК используйте cwl_status)."

    apm = war.get("attacksPerMember", 2)
    _record_war(war, tag, "war", apm)
    c, o = war["clan"], war["opponent"]
    lines = [f"## Война: {c['name']} vs {o['name']}",
             f"{WAR_STATES.get(state, state)} · {war['teamSize']}×{war['teamSize']} · атак на игрока: {apm}"]
    if state == "preparation":
        lines.append(f"До начала боя: {_fmt_left(_parse(war['startTime']))}")
    elif state == "inWar":
        lines.append(f"До конца войны: {_fmt_left(_parse(war['endTime']))}")
    lines.append(f"Счёт: ⭐{c.get('stars', 0)} ({c.get('destructionPercentage', 0):.1f}%) — "
                 f"⭐{o.get('stars', 0)} ({o.get('destructionPercentage', 0):.1f}%)")
    if state == "warEnded":
        ours = (c.get("stars", 0), c.get("destructionPercentage", 0))
        theirs = (o.get("stars", 0), o.get("destructionPercentage", 0))
        lines.append("Итог: " + ("Победа 🏆" if ours > theirs else "Поражение" if ours < theirs else "Ничья"))

    opp_sorted = sorted(o.get("members", []), key=lambda m: m["mapPosition"])
    opp_pos = {m["tag"]: (i, m.get("townhallLevel")) for i, m in enumerate(opp_sorted, 1)}
    # Когда впервые увидели каждую атаку — для «зеркала» (атака на свой номер в первые 12 часов боя)
    seen_all = _load(f"attack_seen_{_key(tag)}.json", {})
    wkey = f"{war.get('preparationStartTime', war.get('startTime'))}_{o['tag']}"
    wseen = seen_all.setdefault(wkey, {})
    now = _now()
    start_dt = _parse(war["startTime"]) if war.get("startTime") else None
    for m in c.get("members", []):
        for a in m.get("attacks", []):
            wseen.setdefault(f"{a['attackerTag']}_{a.get('order')}", now.isoformat())
    for old in sorted(seen_all)[:-6]:
        seen_all.pop(old, None)
    _save(f"attack_seen_{_key(tag)}.json", seen_all)

    rows, missing = [], []
    for i, m in enumerate(sorted(c.get("members", []), key=lambda m: m["mapPosition"]), 1):
        attacks = m.get("attacks", [])
        if state == "preparation" or not start_dt:
            mirror = "—"
        else:
            hit = any(
                opp_pos.get(a["defenderTag"], (None,))[0] == i
                and datetime.fromisoformat(wseen.get(f"{a['attackerTag']}_{a.get('order')}", now.isoformat())) - start_dt <= timedelta(hours=12)
                for a in attacks)
            mirror = "да" if hit else ("пока нет" if state == "inWar" and now - start_dt < timedelta(hours=12) else "нет")
        desc = ", ".join(
            f"№{opp_pos.get(a['defenderTag'], ('?', '?'))[0]} (ТХ{opp_pos.get(a['defenderTag'], ('?', '?'))[1]}): "
            f"{a['stars']}⭐ {a['destructionPercentage']}%" for a in attacks) or "—"
        rows.append([i, m["name"], m.get("townhallLevel"), f"{len(attacks)}/{apm}", desc, mirror])
        if state != "preparation" and len(attacks) < apm:
            missing.append(f"{m['name']} (осталось {apm - len(attacks)})")
    lines += ["", _table(["№", "Игрок", "ТХ", "Атаки", "Результаты", "Зеркало"], rows), ""]
    if state == "inWar":
        lines.append("**Ещё не провели все атаки:** " + (", ".join(missing) if missing else "все отатаковали ✅"))
    elif state == "warEnded":
        lines.append("**Не использовали атаки:** " + (", ".join(missing) if missing else "никто, все молодцы ✅"))
    return "\n".join(lines)


@tool
def war_log(limit: int = 10) -> str:
    """Журнал последних клановых войн: результат, счёт, соперник."""
    tag = _clan_tag()
    try:
        data = api_get(f"/clans/{_enc(tag)}/warlog?limit={int(limit)}")
    except CocError as e:
        if e.code == 403:
            return "Журнал войн клана закрыт — откройте его в настройках клана."
        raise
    rows = []
    for w in data.get("items", []):
        c, o = w["clan"], w["opponent"]
        rows.append([_fmt_dt(_parse(w["endTime"])), WAR_RESULTS.get(w.get("result"), "ЛВК"),
                     f"{w.get('teamSize')}×{w.get('teamSize')}",
                     f"⭐{c.get('stars')} ({c.get('destructionPercentage', 0):.1f}%)",
                     f"{o.get('name', '—')} ⭐{o.get('stars', '?')}"])
    return "## Журнал войн\n" + _table(["Окончание", "Итог", "Размер", "Мы", "Соперник"], rows)


@tool
def missed_attacks_history(last_wars: int = 10) -> str:
    """Кто регулярно пропускает атаки: статистика по последним сохранённым войнам
    (КВ и раунды ЛВК). Данные копятся, когда вызываются current_war_status/cwl_status."""
    tag = _clan_tag()
    wars = _load(f"wars_{_key(tag)}.json", {})
    finished = [w for w in wars.values() if w["state"] == "warEnded"]
    finished.sort(key=lambda w: w.get("end") or "", reverse=True)
    finished = finished[: int(last_wars)]
    if not finished:
        return ("Пока нет сохранённых завершённых войн. История появляется, если вызывать "
                "current_war_status / cwl_status во время войн.")
    current = {m["tag"] for m in _get_clan().get("memberList", [])}
    stats: dict[str, dict] = {}
    for w in finished:
        for t, m in w["members"].items():
            s = stats.setdefault(t, {"name": m["name"], "wars": 0, "used": 0, "possible": 0, "stars": 0})
            s["wars"] += 1
            s["used"] += m["used"]
            s["possible"] += w["attacks_per_member"]
            s["stars"] += m["stars"]
    rows = []
    for t, s in sorted(stats.items(), key=lambda kv: kv[1]["possible"] - kv[1]["used"], reverse=True):
        missed = s["possible"] - s["used"]
        avg = f"{s['stars'] / s['used']:.2f}" if s["used"] else "—"
        rows.append([s["name"] + ("" if t in current else " (вышел)"), s["wars"],
                     f"{s['used']}/{s['possible']}", missed, avg])
    return (f"## Пропущенные атаки за последние {len(finished)} войн\n"
            + _table(["Игрок", "Войн", "Атак", "Пропущено", "Ср. звёзд"], rows))


@tool
def cwl_status() -> str:
    """Лига клановых войн (ЛВК): раунды, соперники, атаки и звёзды каждого участника,
    кто пропустил атаки."""
    tag = _clan_tag()
    try:
        group = api_get(f"/clans/{_enc(tag)}/currentwar/leaguegroup")
    except CocError as e:
        if e.code == 404:
            return "Клан сейчас не участвует в ЛВК."
        raise
    lines = [f"## ЛВК, сезон {group.get('season')} · статус: {group.get('state')}",
             "Кланы в группе: " + ", ".join(c["name"] for c in group.get("clans", []))]
    round_rows, stats, pending = [], {}, []
    for i, rnd in enumerate(group.get("rounds", []), 1):
        for war_tag in rnd.get("warTags", []):
            if war_tag == "#0":
                continue
            w = api_get(f"/clanwarleagues/wars/{_enc(war_tag)}")
            if w["clan"]["tag"] == tag:
                ours, opp = w["clan"], w["opponent"]
            elif w["opponent"]["tag"] == tag:
                ours, opp = w["opponent"], w["clan"]
            else:
                continue
            _record_war(w, tag, "cwl", 1)
            round_rows.append([i, opp["name"], WAR_STATES.get(w["state"], w["state"]),
                               f"⭐{ours.get('stars', 0)} ({ours.get('destructionPercentage', 0):.1f}%)",
                               f"⭐{opp.get('stars', 0)} ({opp.get('destructionPercentage', 0):.1f}%)"])
            for m in ours.get("members", []):
                s = stats.setdefault(m["tag"], {"name": m["name"], "th": m.get("townhallLevel"),
                                                "lineup": 0, "attacks": 0, "stars": 0, "missed": []})
                attacks = m.get("attacks", [])
                s["lineup"] += 1
                s["attacks"] += len(attacks)
                s["stars"] += sum(a["stars"] for a in attacks)
                if not attacks and w["state"] == "warEnded":
                    s["missed"].append(i)
                if not attacks and w["state"] == "inWar":
                    pending.append(m["name"])
            break
    lines += ["", "### Раунды", _table(["Раунд", "Соперник", "Статус", "Мы", "Они"], round_rows), ""]
    rows = [[s["name"], s["th"], s["lineup"], s["attacks"], s["stars"],
             ", ".join(map(str, s["missed"])) or "—"]
            for s in sorted(stats.values(), key=lambda s: -s["stars"])]
    lines += ["### Участники", _table(["Игрок", "ТХ", "В составе", "Атак", "Звёзд", "Пропустил раунды"], rows), ""]
    lines.append("**Не атаковали в текущем раунде:** " + (", ".join(pending) if pending else "все отатаковали / нет активного раунда"))
    return "\n".join(lines)


@tool
def raid_weekend() -> str:
    """Последние рейды столицы клана: добыча, атаки каждого участника
    и кто из клана не участвовал."""
    tag = _clan_tag()
    data = api_get(f"/clans/{_enc(tag)}/capitalraidseasons?limit=1")
    items = data.get("items", [])
    if not items:
        return "Нет данных о рейдах столицы."
    r = items[0]
    lines = [f"## Рейды столицы: {_fmt_dt(_parse(r['startTime']))} — {_fmt_dt(_parse(r['endTime']))} "
             f"({'идут' if r.get('state') == 'ongoing' else 'завершены'})",
             f"Добыча: {r.get('capitalTotalLoot', 0)} · атак: {r.get('totalAttacks', 0)} · "
             f"районов разрушено: {r.get('enemyDistrictsDestroyed', 0)} · рейдов завершено: {r.get('raidsCompleted', 0)}"]
    if r.get("state") != "ongoing":
        lines.append(f"Награда: {r.get('offensiveReward', 0)} (атака) + {r.get('defensiveReward', 0)} (защита) медалей рейдов")
    members = sorted(r.get("members", []), key=lambda m: -m.get("capitalResourcesLooted", 0))
    rows = [[m["name"], f"{m.get('attacks', 0)}/{m.get('attackLimit', 5) + m.get('bonusAttackLimit', 0)}",
             m.get("capitalResourcesLooted", 0)] for m in members]
    lines += ["", _table(["Игрок", "Атаки", "Добыча"], rows), ""]
    participated = {m["tag"] for m in members}
    absent = [m["name"] for m in _get_clan().get("memberList", []) if m["tag"] not in participated]
    lines.append(f"**Не участвовали ({len(absent)}):** " + (", ".join(absent) if absent else "все участвовали ✅"))
    return "\n".join(lines)


@tool
def war_roster() -> str:
    """Готовность к войне: у каждого участника ратуша, сумма уровней героев,
    звёзды КВ и статус «участвую/не участвую в войне». Полезно для подбора состава."""
    clan = _get_clan()
    rows, opted_in = [], 0
    players = []
    for m in clan.get("memberList", []):
        p = api_get(f"/players/{_enc(m['tag'])}")
        heroes = sum(h.get("level", 0) for h in p.get("heroes", []) if h.get("village") == "home")
        players.append((p.get("townHallLevel", 0), heroes, p))
    for th, heroes, p in sorted(players, key=lambda x: (-x[0], -x[1])):
        pref = p.get("warPreference")
        opted_in += pref == "in"
        rows.append([p["name"], th, heroes, p.get("warStars", 0),
                     "✅ участвует" if pref == "in" else "❌ не участвует" if pref == "out" else "?"])
    return (f"## Состав для войны — готовы участвовать: {opted_in} из {len(players)}\n"
            + _table(["Игрок", "ТХ", "Сумма героев", "Звёзд КВ", "Статус"], rows))


@tool
def player_info(player_tag: str) -> str:
    """Подробно об игроке по тегу: ратуша, герои, звёзды КВ, атаки и защиты,
    донаты, статус участия в войне."""
    return _player_md(api_get(f"/players/{_enc(_normalize_tag(player_tag))}"))


def _player_md(p: dict) -> str:
    clan = p.get("clan", {})
    lines = [
        f"## {p['name']} ({p['tag']})",
        f"Клан: {clan.get('name', '—')} · роль: {ROLES.get(p.get('role'), p.get('role', '—'))}",
        f"ТХ {p.get('townHallLevel')}" + (f" (оружие ур. {p['townHallWeaponLevel']})" if p.get("townHallWeaponLevel") else "")
        + f" · уровень опыта {p.get('expLevel')}",
        f"Кубки: {p.get('trophies')} (рекорд {p.get('bestTrophies')}) · лига: {p.get('league', {}).get('name', '—')}",
        f"Звёзд КВ: {p.get('warStars', 0)} · участие в войне: "
        + {"in": "да", "out": "нет"}.get(p.get("warPreference"), "?"),
        f"Побед в атаках за сезон: {p.get('attackWins', 0)} · успешных защит: {p.get('defenseWins', 0)}",
        f"Донаты за сезон: отдал {p.get('donations', 0)}, получил {p.get('donationsReceived', 0)}",
        f"Вклад в столицу клана: {p.get('clanCapitalContributions', 0)}",
        "",
    ]
    def lvl_rows(items):
        return [[x["name"], f"{x.get('level')}/{x.get('maxLevel')}"] for x in items]
    home = lambda items: [x for x in items if x.get("village") == "home"]
    heroes = home(p.get("heroes", []))
    lines.append("### Герои\n" + _table(["Герой", "Уровень"], lvl_rows(heroes)))
    equip = home(p.get("heroEquipment", []))
    worn = {e.get("name"): h["name"] for h in heroes for e in (h.get("equipment") or [])}
    if equip:
        lines.append("\n### Снаряжение героев\n" + _table(["Снаряжение", "Уровень", "Надето"],
            [[e["name"], f"{e.get('level')}/{e.get('maxLevel')}", worn.get(e["name"], "—")] for e in equip]))
    PETS = {"L.A.S.S.I", "Electro Owl", "Mighty Yak", "Unicorn", "Frosty", "Diggy", "Poison Lizard",
            "Phoenix", "Spirit Fox", "Angry Jelly", "Sneezy", "Greedy Raven"}
    SIEGE = {"Wall Wrecker", "Battle Blimp", "Stone Slammer", "Siege Barracks", "Log Launcher",
             "Flame Flinger", "Battle Drill", "Troop Launcher"}
    troops = [t for t in home(p.get("troops", [])) if "superTroopIsActive" not in t]
    pets = [t for t in troops if t["name"] in PETS]
    siege = [t for t in troops if t["name"] in SIEGE]
    army = [t for t in troops if t["name"] not in PETS and t["name"] not in SIEGE]
    supers = [t for t in home(p.get("troops", [])) if t.get("superTroopIsActive")]
    if pets:
        lines.append("\n### Питомцы\n" + _table(["Питомец", "Уровень"], lvl_rows(pets)))
    lines.append("\n### Войска\n" + _table(["Войско", "Уровень"], lvl_rows(army)))
    if siege:
        lines.append("\n### Осадные машины\n" + _table(["Машина", "Уровень"], lvl_rows(siege)))
    spells = home(p.get("spells", []))
    if spells:
        lines.append("\n### Заклинания\n" + _table(["Заклинание", "Уровень"], lvl_rows(spells)))
    if supers:
        lines.append("\n**Активные супервойска:** " + ", ".join(t["name"] for t in supers))
    return "\n".join(lines)


@tool
def export_players() -> str:
    """Сохраняет армию (герои, снаряжение, питомцы, войска, машины, заклинания) всех
    участников клана в файлы players_export/<ТЕГ>.json рядом с программой —
    для загрузки на панель клана. Возвращает короткую сводку."""
    import json as _json, time as _time
    out = Path(__file__).resolve().parent / "players_export"
    out.mkdir(exist_ok=True)
    clan = _get_clan()
    now = int(_time.time() * 1000)
    ok, bad = 0, []
    for m in clan.get("memberList", []):
        try:
            md = _player_md(api_get(f"/players/{_enc(m['tag'])}"))
            (out / (m["tag"].lstrip("#").upper() + ".json")).write_text(
                _json.dumps({"md": md, "at": now}, ensure_ascii=False), encoding="utf-8")
            ok += 1
        except Exception as e:  # noqa: BLE001
            bad.append(f"{m.get('name')}: {e}")
    return f"Сохранено игроков: {ok} в {out}" + (f"\nОшибки: {'; '.join(bad)}" if bad else "")


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #

def serve() -> None:
    try:
        from mcp.server.mcpserver import MCPServer as Server  # mcp 2.x
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x
        except ImportError:
            sys.exit("Не установлен пакет mcp. Выполните: pip install mcp")
    server = Server("clash-of-clans-clan")
    for fn in TOOLS:
        server.tool()(fn)
    server.run()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "check":
        try:
            clan = _get_clan()
            _update_activity(clan)
            print(f"✅ Всё работает: {clan['name']} ({clan['tag']}), участников: {clan.get('members')}")
        except CocError as e:
            sys.exit(f"❌ {e}")
    elif cmd == "report":
        for fn in (clan_overview, member_activity, current_war_status, raid_weekend):
            print(fn(), "\n")
    elif cmd == "serve":
        serve()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
