"""Аналитика погодных уведомлений (чистые функции, без Android/сети).

Этот модуль — единственный источник правил: Java-воркер
(WeatherUpdateWorker) зеркалит те же типы, пороги и тексты, потому что
уведомления должны приходить, когда приложение закрыто, а Python-процесс
в этот момент не запущен. Пороги продублированы константами и совпадают
(это проверяется тестом сверки исходников).

Правила намеренно осторожны в формулировках: не обещаем точное время
начала дождя/снега — только «в ближайшие часы возможен…». Оценка
ориентировочная, исходя из прогноза MET Norway.
"""

import json
import os
import time

# --- Типы уведомлений (ключ -> категория настройки) -------------------------
ALERT_RAIN = "rain"
ALERT_SNOW = "snow"
ALERT_THUNDER = "thunder"
ALERT_WIND = "wind"
ALERT_TEMP_DROP = "temp_drop"
ALERT_TEMP_RISE = "temp_rise"
ALERT_HEAT = "heat"
ALERT_FROST = "frost"
ALERT_PRESSURE = "pressure"

ALL_TYPES = (
    ALERT_RAIN, ALERT_SNOW, ALERT_THUNDER, ALERT_WIND,
    ALERT_TEMP_DROP, ALERT_TEMP_RISE, ALERT_HEAT, ALERT_FROST,
    ALERT_PRESSURE,
)

# Категории настроек: «temperature» управляет всеми температурами,
# «pressure» — перепадами давления.
CATEGORY = {
    ALERT_RAIN: "rain",
    ALERT_SNOW: "snow",
    ALERT_THUNDER: "thunder",
    ALERT_WIND: "wind",
    ALERT_TEMP_DROP: "temperature",
    ALERT_TEMP_RISE: "temperature",
    ALERT_HEAT: "temperature",
    ALERT_FROST: "temperature",
    ALERT_PRESSURE: "pressure",
}

# Критичные (проходят даже в тихом режиме ночью): жары/мороз/гроза.
CRITICAL = (ALERT_HEAT, ALERT_FROST, ALERT_THUNDER)

# --- Пороги (зеркалятся в Java: см. WeatherUpdateWorker) --------------------
LOOKAHEAD_SHORT_H = 6      # окно «дождь/снег/гроза/ветер/скачок температуры»
LOOKAHEAD_LONG_H = 24      # окно экстремумов (жара/мороз)
WIND_ALERT_MPS = 12.0      # сильный ветер, м/с
GUST_ALERT_MPS = 15.0      # опасные порывы, м/с
TEMP_SHIFT_C = 6.0         # резкое похолодание/потепление, °C
HEAT_C = 30.0              # экстремальная жара, °C
FROST_C = -25.0            # сильный мороз, °C
PRESSURE_SHIFT_HPA = 8.0   # резкий перепад давления за ~3 ч, гПа
COOLDOWN_SECONDS = 3 * 3600  # не повторять один и тот же тип раньше 3 часов
QUIET_START_H = 23         # тихий режим: с 23:00
QUIET_END_H = 7            # ... до 07:00 (локального времени)

# Тексты (осторожные: без обещаний точного времени).
TEXT = {
    ALERT_RAIN: ("Дождь",
                 "В ближайшие часы возможен дождь."),
    ALERT_SNOW: ("Снегопад",
                 "В ближайшие часы возможен снегопад."),
    ALERT_THUNDER: ("Гроза",
                    "В ближайшие часы возможна гроза."),
    ALERT_WIND: ("Сильный ветер",
                 "Ожидается усиление ветра."),
    ALERT_TEMP_DROP: ("Резкое похолодание",
                      "В ближайшие часы возможно резкое похолодание."),
    ALERT_TEMP_RISE: ("Резкое потепление",
                      "В ближайшие часы возможно резкое потепление."),
    ALERT_HEAT: ("Экстремальная жара",
                 "Ожидается сильная жара."),
    ALERT_FROST: ("Сильный мороз",
                  "Ожидается сильный мороз."),
    ALERT_PRESSURE: ("Резкий перепад давления",
                     "В ближайшие часы возможно резкое изменение давления."),
}

SETTINGS_FILE = "wildvantage_notify.json"

DEFAULT_SETTINGS = {
    "enabled": False,       # мастер-переключатель (по умолчанию выключено)
    "rain": True,
    "snow": True,
    "thunder": True,
    "wind": True,
    "temperature": True,
    "pressure": True,
    "quiet": False,         # тихий режим (ночью — только критичное)
}


def default_settings():
    return dict(DEFAULT_SETTINGS)


def normalize_settings(data):
    """Привести произвольный dict к известной схеме (значения bool)."""
    out = default_settings()
    if isinstance(data, dict):
        for key in out:
            if key in data and isinstance(data[key], bool):
                out[key] = data[key]
    return out


def load_settings(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return normalize_settings(data)
    except Exception:
        return default_settings()


def save_settings(path, settings):
    """Атомарная запись (tmp + fsync + os.replace), как у прочих файлов."""
    tmp = path + ".tmp"
    payload = normalize_settings(settings)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


# --- Анализ прогноза ---------------------------------------------------------

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _sym(code):
    return str(code or "").lower()


def _is_thunder(code):
    return "thunder" in _sym(code)


def _is_snow(code):
    s = _sym(code)
    return ("snow" in s) or ("sleet" in s) or ("graupel" in s)


def _is_rain(code):
    s = _sym(code)
    return ("rain" in s) and not _is_snow(code)


def _window(hours, now_iso, span_h):
    """Записи с локальным временем в пределах [now, now+span_h]."""
    out = []
    if not isinstance(hours, (list, tuple)):
        return out
    cur = str(now_iso or "")[:13]
    for h in hours:
        if not isinstance(h, dict):
            continue
        t = str(h.get("time") or "")
        if len(t) < 13:
            continue
        key = t[:13]
        if not cur or key >= cur:
            out.append(h)
        if len(out) >= span_h + 1:
            break
    if not out:
        out = [h for h in hours if isinstance(h, dict)][:span_h + 1]
    return out


def evaluate_alerts(weather, now_iso=None):
    """Список сработавших уведомлений по текущей погоде/прогнозу.

    Возвращает список dict: {type, category, title, body, critical}.
    Ничего не выдумывает: при отсутствии данных соответствующий тип просто
    не срабатывает.
    """
    alerts = []
    if not isinstance(weather, dict):
        return alerts
    cur = weather.get("current") or {}
    hours = weather.get("hours") or []
    if not now_iso:
        now_iso = str(cur.get("time") or "")

    short = _window(hours, now_iso, LOOKAHEAD_SHORT_H)
    long = _window(hours, now_iso, LOOKAHEAD_LONG_H)

    def add(kind):
        title, body = TEXT[kind]
        alerts.append({
            "type": kind,
            "category": CATEGORY[kind],
            "title": title,
            "body": body,
            "critical": kind in CRITICAL,
        })

    # Дождь / снег / гроза — по символам ближайших часов (и текущего).
    syms = [_sym(cur.get("code"))] + [_sym(h.get("code")) for h in short]
    if any(_is_thunder(c) for c in syms):
        add(ALERT_THUNDER)
    if any(_is_snow(c) for c in syms):
        add(ALERT_SNOW)
    elif any(_is_rain(c) for c in syms):
        add(ALERT_RAIN)

    # Ветер и порывы.
    winds = [w for w in (_num(h.get("wind_speed")) for h in short)
             if w is not None]
    gusts = [g for g in (_num(cur.get("wind_gust")) or 0.0,
                          *[_num(h.get("wind_gust")) or 0.0 for h in short])]
    if winds and (max(winds) >= WIND_ALERT_MPS
                  or (gusts and max(gusts) >= GUST_ALERT_MPS)):
        add(ALERT_WIND)

    # Температурные скачки (в коротком окне).
    temps = [t for t in (_num(h.get("temp")) for h in short) if t is not None]
    cur_t = _num(cur.get("temp"))
    if cur_t is None and temps:
        cur_t = temps[0]
    if cur_t is not None and temps:
        if min(temps) <= cur_t - TEMP_SHIFT_C:
            add(ALERT_TEMP_DROP)
        if max(temps) >= cur_t + TEMP_SHIFT_C:
            add(ALERT_TEMP_RISE)

    # Экстремумы (в длинном окне).
    temps_long = [t for t in (_num(h.get("temp")) for h in long)
                  if t is not None]
    if cur_t is not None:
        temps_long.append(cur_t)
    if temps_long:
        if max(temps_long) >= HEAT_C:
            add(ALERT_HEAT)
        if min(temps_long) <= FROST_C:
            add(ALERT_FROST)

    # Перепад давления: текущее против ближайшего «через ~3 ч».
    p_now = _num(cur.get("pressure"))
    if p_now is None and short:
        p_now = _num(short[0].get("pressure"))
    p3 = None
    if len(short) > 3:
        p3 = _num(short[3].get("pressure"))
    if p_now is not None and p3 is not None and abs(p3 - p_now) >= PRESSURE_SHIFT_HPA:
        add(ALERT_PRESSURE)

    return alerts


def category_enabled(category, settings):
    if not isinstance(settings, dict):
        return False
    if not settings.get("enabled", False):
        return False
    return bool(settings.get(category, True))


def quiet_now(settings, local_hour):
    """True — тихий режим сейчас (ночью), не критичные уведомления молчат."""
    if not isinstance(settings, dict) or not settings.get("quiet", False):
        return False
    try:
        h = int(local_hour) % 24
    except (TypeError, ValueError):
        return False
    if QUIET_START_H <= QUIET_END_H:
        return QUIET_START_H <= h < QUIET_END_H
    return h >= QUIET_START_H or h < QUIET_END_H


def cooldown_ok(state, kind, now=None, cooldown=COOLDOWN_SECONDS):
    """Не повторять один тип раньше cooldown секунд (state — {type: epoch})."""
    if not isinstance(state, dict):
        return True
    try:
        last = float(state.get(kind, 0))
    except (TypeError, ValueError):
        last = 0.0
    now = time.time() if now is None else now
    return (now - last) >= cooldown


def select_notifications(alerts, settings, state=None, now=None, local_hour=None):
    """Отфильтровать по настройкам/тихому режиму/cooldown.

    Возвращает (to_send, new_state): new_state — обновлённый словарь
    last-sent (для типов из to_send проставляется текущий момент).
    """
    state = dict(state) if isinstance(state, dict) else {}
    now = time.time() if now is None else now
    to_send = []
    for a in alerts or []:
        if not isinstance(a, dict):
            continue
        kind = a.get("type")
        if kind not in ALL_TYPES:
            continue
        if not category_enabled(a.get("category"), settings):
            continue
        if quiet_now(settings, local_hour) and not a.get("critical"):
            continue
        if not cooldown_ok(state, kind, now=now):
            continue
        to_send.append(a)
        state[kind] = now
    return to_send, state
