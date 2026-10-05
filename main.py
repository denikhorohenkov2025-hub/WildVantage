import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone

from kivy.clock import Clock
from kivy.utils import platform
from kivy.lang import Builder
from kivy.metrics import dp
from kivymd.app import MDApp
from kivymd.uix.list import ThreeLineIconListItem, IconLeftWidget
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.floatlayout import MDFloatLayout
from kivymd.uix.label import MDLabel, MDIcon

# GPS — отдельным try/except, чтобы сбой импорта не ронял приложение
try:
    from plyer import gps
except Exception:
    gps = None
try:
    import certifi
except Exception:
    certifi = None
# Графика нужна только для выделения текущего часа (заглушка в тестах без GL).
try:
    from kivy.graphics import Color, RoundedRectangle
except Exception:
    Color = None
    RoundedRectangle = None

CACHE_FILE = "wildvantage_v4.json"
# Сырой ответ Open-Meteo, который атомарно пишет фоновый Java-воркер
# (WorkManager) рядом с кэшем; Python принимает его в consume_bg_raw.
BG_RAW_FILE = "wildvantage_bg_raw.json"
NOMINATIM_UA = "WildVantage (Android weather app; contact: denikhorohenkov2025-hub)"

# Сбор GPS: ищем лучший fix, не берём первый грубый.
GPS_SEARCH_TIMEOUT = 28   # максимум ожидания хорошего fix, сек
GPS_GOOD_ACCURACY = 10.0  # при <=10 м завершаем сразу
GPS_OK_ACCURACY = 20.0    # при <=20 м можно принять, если нет улучшения
GPS_SETTLE_TIME = 6.0     # сколько ждать улучшения при <=20 м, сек
GPS_STALE_AGE = 8.0       # fix старше этого возраста отбрасываем, сек

# Погода: Open-Meteo (бесплатно, без ключа).
# current: температура, ощущается, влажность, ветер, код WMO, облачность, день/ночь
# hourly:  почасовая температура/осадки/день-ночь — для «Ближайшие 24 ч»
# daily:   на 7 дней: код WMO, tmax/tmin, восход/закат, вероятность осадков, ветер
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,weather_code,cloud_cover,is_day,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    "&hourly=temperature_2m,weather_code,is_day,apparent_temperature,precipitation_probability"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,precipitation_probability_max,wind_speed_10m_max"
    "&timezone=auto&forecast_days=7"
)
GEOCODE_URL = (
    "https://geocoding-api.open-meteo.com/v1/search"
    "?name={q}&count=1&language=ru&format=json"
)
REVERSE_URL = (
    "https://nominatim.openstreetmap.org/reverse"
    "?format=jsonv2&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=ru"
)

# --- СЕТЬ: классификация ошибок, устойчивость к сломанному IPv6 ---------------
# Симптом с устройства: «с VPN работает, без VPN — Нет сети». Классическая причина —
# на мобильной сети без VPN IPv6-маршрут чёрной дырой держит соединение до
# таймаута, браузер это переживает (Happy Eyeballs), а Python-стек без контроля
# адреса — нет. Здесь DNS резолвится вручную, IPv4 пробуется ПЕРВЫМ, IPv6
# остаётся запасным вариантом; каждый адрес получает свой короткий таймаут.
NET_CONNECT_PER_ADDR = 6.0   # лимит одного адреса, сек (не съедает весь бюджет)
NET_MAX_ADDR_ATTEMPTS = 4    # сколько адресов пробуем максимум
PROBE_TARGETS = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 443),
    ("2606:4700:4700::1111", 443),
    ("api.open-meteo.com", 443),
    ("nominatim.openstreetmap.org", 443),
)


class NetError(Exception):
    """Классифицированная сетевая ошибка.

    kind: dns | timeout | connect | tls | http | json | unknown
    online: None — не проверяли, True/False — результат пробы «есть ли сеть».
    Секреты (заголовки, тела) в сообщение не попадают.
    """

    def __init__(self, kind, host="", detail="", status=None):
        super().__init__(f"net[{kind}] {host} {detail}".strip())
        self.kind = kind
        self.host = host
        self.detail = detail
        self.status = status
        self.online = None


def net_error_text(err, service="weather"):
    """Пользовательский текст по классифицированной ошибке (без секретов).

    «Нет интернета» показывается ТОЛЬКО когда проба сети подтвердила офлайн —
    любая HTTP-ошибка не считается отсутствием интернета.
    """
    if getattr(err, "online", None) is False:
        return "Нет интернета"
    if getattr(err, "kind", None) == "timeout":
        return "Сервис не отвечает"
    if service == "weather":
        return "Сервис погоды недоступен"
    if service == "nominatim":
        return "Не удалось уточнить название места"
    if service == "geocode":
        return "Сервис поиска недоступен"
    return "Сервис не отвечает"


def resolve_addrs(host, port=443):
    """DNS -> адреса подключения, IPv4 ПЕРВЫМИ (см. комментарий выше)."""
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    v4, v6, seen = [], [], set()
    for info in infos:
        key = info[4][:2]
        if key in seen:
            continue
        seen.add(key)
        (v4 if info[0] == socket.AF_INET else v6).append(info)
    addrs = v4 + v6
    if not addrs:
        raise socket.gaierror(socket.EAI_NONAME, "no addresses for host")
    return addrs


def _tls_context():
    """TLS-контекст с обязательной проверкой сертификата (verify=True)."""
    try:
        if certifi is not None:
            return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    return ssl.create_default_context()


def _open_connection(addrinfo, server_hostname, timeout, context):
    """TCP + TLS к конкретному адресу; SNI — исходный хост из URL."""
    fam, stype, proto, _canon, sa = addrinfo
    raw = socket.socket(fam, stype, proto)
    try:
        raw.settimeout(timeout)
        raw.connect(sa)
        if context is not None:
            return context.wrap_socket(raw, server_hostname=server_hostname)
        return raw
    except Exception:
        raw.close()
        raise


def net_get(url, timeout=12.0, headers=None, resolver=None, opener=None):
    """GET с раздельной диагностикой: dns / connect / tls / timeout / http.

    resolver(host, port) -> [addrinfo, ...]; opener(addrinfo, host, timeout,
    context) -> TLS-сокет — подменяются в тестах для имитации сценариев сети.
    4xx/5xx НЕ ретраятся: сервер ответил, адреса пробовать незачем.
    До 2 проходов по адресам (ограниченный retry со свежим DNS), общий
    бюджет timeout на оба прохода — бесконечных циклов нет.
    """
    resolver = resolver or resolve_addrs
    opener = opener or _open_connection
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    use_tls = parts.scheme == "https"
    context = _tls_context() if use_tls else None
    conn_cls = http.client.HTTPSConnection if use_tls else http.client.HTTPConnection
    deadline = time.monotonic() + timeout
    errors = []

    for pass_no in range(2):
        try:
            addrs = resolver(host, port)
        except NetError:
            raise
        except OSError as e:
            raise NetError("dns", host, f"{type(e).__name__}: {e}") from e
        except Exception as e:
            raise NetError("dns", host, f"{type(e).__name__}: {e}") from e

        for addr in addrs[:NET_MAX_ADDR_ATTEMPTS]:
            ip = str(addr[4][0])
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise NetError("timeout", host, "; ".join(errors) or "deadline")
            per = min(NET_CONNECT_PER_ADDR, remain)
            try:
                sock = opener(addr, host, per, context)
            except ssl.SSLError as e:
                errors.append(f"tls[{ip}]: {type(e).__name__}: {e}")
                continue
            except (socket.timeout, TimeoutError):
                errors.append(f"timeout[{ip}]")
                continue
            except OSError as e:
                errors.append(f"connect[{ip}]: {type(e).__name__}: errno={getattr(e, 'errno', None)}")
                continue
            except Exception as e:
                errors.append(f"open[{ip}]: {type(e).__name__}: {e}")
                continue
            try:
                conn = conn_cls(host, port, timeout=max(0.5, deadline - time.monotonic()))
                conn.sock = sock
                conn.request("GET", path, headers=headers or {})
                resp = conn.getresponse()
                status = resp.status
                body = resp.read(2_000_000)
                conn.close()
            except (socket.timeout, TimeoutError) as e:
                errors.append(f"read-timeout[{ip}]: {e}")
                try:
                    sock.close()
                except Exception:
                    pass
                continue
            except OSError as e:
                errors.append(f"io[{ip}]: {type(e).__name__}: {e}")
                try:
                    sock.close()
                except Exception:
                    pass
                continue
            if 200 <= status < 300:
                return status, body
            raise NetError("http", host, f"HTTP {status}", status=status)

        # Проход не удался. Второй (последний) — только если есть бюджет.
        if pass_no == 0 and errors and deadline - time.monotonic() > 0.5:
            errors.append("retry-pass")
            continue
        break

    joined = "; ".join(errors)
    if any(e.startswith(("timeout", "read-timeout")) for e in errors):
        raise NetError("timeout", host, joined)
    raise NetError("connect", host, joined)


def net_get_json(url, timeout=12.0, headers=None, resolver=None, opener=None):
    """net_get + разбор JSON. Некорректный JSON -> NetError(kind='json')."""
    status, body = net_get(url, timeout=timeout, headers=headers,
                           resolver=resolver, opener=opener)
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        return status, json.loads(body.decode("utf-8"))
    except Exception as e:
        raise NetError("json", host, f"{type(e).__name__}: {e}") from e


def probe_online(timeout=1.5, targets=None, total=None):
    """Быстрая проба «есть ли вообще сеть»: TCP до независимых адресов.

    Нужна, чтобы НЕ называть сервис/HTTP-ошибку отсутствием интернета
    и наоборот — чтобы реальный офлайн показать как «Нет интернета».

    Общий бюджет total (по умолчанию max(timeout, 2.5) с) ограничивает
    суммарное время всех целей: даже при мёртвых адресах проба не
    «зависает» на десятки секунд и не блокирует статус.
    """
    deadline = time.monotonic() + (float(total) if total else max(timeout, 2.5))
    for host, port in (targets or PROBE_TARGETS):
        remain = deadline - time.monotonic()
        if remain <= 0:
            return False
        try:
            sock = socket.create_connection((host, port), min(timeout, remain))
            sock.close()
            return True
        except OSError:
            continue
    return False

# WMO weather_code -> (описание, иконка днём, иконка ночью)
WMO = {
    0: ("Ясно", "weather-sunny", "weather-night"),
    1: ("Преимущественно ясно", "weather-sunny", "weather-night"),
    2: ("Переменная облачность", "weather-partly-cloudy", "weather-night-partly-cloudy"),
    3: ("Пасмурно", "weather-cloudy", "weather-cloudy"),
    45: ("Туман", "weather-fog", "weather-fog"),
    48: ("Туман с изморозью", "weather-fog", "weather-fog"),
    51: ("Слабая морось", "weather-partly-rainy", "weather-partly-rainy"),
    53: ("Морось", "weather-rainy", "weather-rainy"),
    55: ("Сильная морось", "weather-pouring", "weather-pouring"),
    56: ("Ледяная морось", "weather-snowy-rainy", "weather-snowy-rainy"),
    57: ("Сильная ледяная морось", "weather-snowy-rainy", "weather-snowy-rainy"),
    61: ("Небольшой дождь", "weather-rainy", "weather-rainy"),
    63: ("Дождь", "weather-pouring", "weather-pouring"),
    65: ("Сильный дождь", "weather-pouring", "weather-pouring"),
    66: ("Ледяной дождь", "weather-snowy-rainy", "weather-snowy-rainy"),
    67: ("Сильный ледяной дождь", "weather-snowy-rainy", "weather-snowy-rainy"),
    71: ("Небольшой снег", "weather-snowy", "weather-snowy"),
    73: ("Снег", "weather-snowy", "weather-snowy"),
    75: ("Сильный снег", "weather-snowy-heavy", "weather-snowy-heavy"),
    77: ("Снежная крупа", "weather-snowy", "weather-snowy"),
    80: ("Небольшой ливень", "weather-partly-rainy", "weather-partly-rainy"),
    81: ("Ливень", "weather-pouring", "weather-pouring"),
    82: ("Сильный ливень", "weather-pouring", "weather-pouring"),
    85: ("Небольшой снегопад", "weather-snowy", "weather-snowy"),
    86: ("Сильный снегопад", "weather-snowy-heavy", "weather-snowy-heavy"),
    95: ("Гроза", "weather-lightning-rainy", "weather-lightning-rainy"),
    96: ("Гроза с градом", "weather-hail", "weather-hail"),
    99: ("Сильная гроза с градом", "weather-hail", "weather-hail"),
}
WMO_FALLBACK = ("Неизвестно", "weather-cloudy", "weather-cloudy")


def wmo_info(code, is_day=True):
    """Код WMO -> (описание, имя иконки) с учётом дня/ночи."""
    try:
        entry = WMO.get(int(code))
    except Exception:
        entry = None
    if entry is None:
        return WMO_FALLBACK[0], (WMO_FALLBACK[1] if is_day else WMO_FALLBACK[2])
    return entry[0], (entry[1] if is_day else entry[2])


def _clean_part(s):
    """Убирает служебные приставки: 'район Новокосино' -> 'Новокосино'."""
    s = (s or "").strip()
    for p in (
        "район ",
        "город ",
        "посёлок ",
        "поселок ",
        "село ",
        "станица ",
        "г. ",
        "пгт ",
        "д. ",
        "с. ",
        "мкр ",
    ):
        if s.lower().startswith(p):
            s = s[len(p):].strip()
    return s or None


def pick_location_name(address):
    """Выбирает наиболее конкретное человеческое название из адреса Nominatim."""
    details = location_pick_details(address)
    return details["name"] if details else None


def _first_set(address, keys):
    """Первый непустой key из списка и его очищенное значение."""
    for k in keys:
        v = address.get(k)
        if isinstance(v, str):
            c = _clean_part(v)
            if c:
                return k, c
    return None, None


# Порядок приоритета для «района» (самое конкретное наверху).
DISTRICT_FIELDS = [
    "neighbourhood",   # соседство, микрорайон (OsmAnd/qName, напр. «Новокосино»)
    "suburb",          # район/квартал города
    "city_district",   # административный округ
    "quarter",         # квартал
    "borough",         # боро
    "district",        # район
    "city_block",      # квартал/блок
    "residential",     # самоназвание жилого комплекса (не район!)
]
LOCALITY_FIELDS = ["city", "town", "village", "municipality", "hamlet", "locality"]
FALLBACK_FIELDS = ["county", "state", "region"]


def location_pick_details(address):
    """Разбор адреса Nominatim: какое поле выбрано для района и города.

    Возвращает dict с ключами:
      district_field / district  — выбранное поле района и значение,
      locality_field / locality  — выбранный город/населённый пункт,
      name                       — итоговое имя для интерфейса (или None).
    """
    if not isinstance(address, dict):
        return None

    dist_key, district = _first_set(address, DISTRICT_FIELDS)
    loc_key, locality = _first_set(address, LOCALITY_FIELDS)

    if district and locality and district.lower() != locality.lower():
        return {
            "district_field": dist_key,
            "district": district,
            "locality_field": loc_key,
            "locality": locality,
            "name": f"{locality}, {district}",
        }
    if district:
        return {"district_field": dist_key, "district": district, "name": district}
    if locality:
        return {"locality_field": loc_key, "locality": locality, "name": locality}
    f_key, f_val = _first_set(address, FALLBACK_FIELDS)
    if f_val:
        return {f_key: f_val, "name": f_val}
    return {"name": None}


def _at(seq, i):
    try:
        return seq[i]
    except Exception:
        return None


def normalize_weather(data):
    """Приводит ответ Open-Meteo к внутренней структуре, независимой от API."""
    if not isinstance(data, dict):
        return None
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    if not isinstance(cur, dict):
        cur = {}
    if not isinstance(daily, dict):
        daily = {}
    dates = daily.get("time") or []
    days = []
    for i in range(len(dates)):
        days.append(
            {
                "date": _at(dates, i),
                "code": _at(daily.get("weather_code"), i),
                "tmax": _at(daily.get("temperature_2m_max"), i),
                "tmin": _at(daily.get("temperature_2m_min"), i),
                "sunrise": _at(daily.get("sunrise"), i),
                "sunset": _at(daily.get("sunset"), i),
                "precip": _at(daily.get("precipitation_probability_max"), i),
                "wind": _at(daily.get("wind_speed_10m_max"), i),
            }
        )
    hourly = data.get("hourly") or {}
    if not isinstance(hourly, dict):
        hourly = {}
    h_times = hourly.get("time") or []
    hours = []
    for i in range(len(h_times)):
        hours.append(
            {
                "time": _at(h_times, i),
                "temp": _at(hourly.get("temperature_2m"), i),
                "code": _at(hourly.get("weather_code"), i),
                "is_day": _at(hourly.get("is_day"), i),
                "feels": _at(hourly.get("apparent_temperature"), i),
                "precip": _at(hourly.get("precipitation_probability"), i),
            }
        )
    return {
        "current": {
            "time": cur.get("time"),
            "temp": cur.get("temperature_2m"),
            "code": cur.get("weather_code"),
            "cloud_cover": cur.get("cloud_cover"),
            "is_day": cur.get("is_day"),
            "feels": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_speed": cur.get("wind_speed_10m"),
            "wind_dir": cur.get("wind_direction_10m"),
            "wind_gust": cur.get("wind_gusts_10m"),
        },
        "hours": hours,
        "daily": days,
        "timezone": data.get("timezone"),
        "utc_offset_seconds": data.get("utc_offset_seconds"),
    }


def fmt_temp(value):
    try:
        return f"{round(float(value))}°"
    except Exception:
        return "--°"


def fmt_hhmm(iso):
    if isinstance(iso, str) and len(iso) >= 16 and "T" in iso:
        return iso[11:16]
    return "--:--"


def hhmm_from_unix(unix, offset=0):
    """HH:MM в timezone места (offset — utc_offset_seconds из ответа API)."""
    try:
        moment = datetime.fromtimestamp(int(unix), tz=timezone.utc)
        moment = moment + timedelta(seconds=int(offset or 0))
        return moment.strftime("%H:%M")
    except Exception:
        return "--:--"


def updated_status_text(last_success=None, fallback_iso=None):
    """«Обновлено: HH:MM» только от последнего УСПЕШНОГО обновления.

    last_success: {"unix": int, "offset": int} — момент успеха fetch в unix и
    сдвиг timezone места. fallback_iso — время данных старого кэша (формат
    Open-Meteo, уже в timezone места). Ошибка время НЕ меняет: сюда оно не
    передаётся вовсе.
    """
    if isinstance(last_success, str) and not isinstance(fallback_iso, str):
        # Совместимость старого вызова updated_status_text(iso): строка — это
        # время данных (fallback), а не момент успеха.
        last_success, fallback_iso = None, last_success
    if isinstance(last_success, dict) and last_success.get("unix") is not None:
        return f"Обновлено: {hhmm_from_unix(last_success.get('unix'), last_success.get('offset'))}"
    if isinstance(fallback_iso, str) and len(fallback_iso) >= 16 and "T" in fallback_iso:
        return f"Обновлено: {fmt_hhmm(fallback_iso)}"
    return "Обновлено: --:--"


def compose_status(reason=None, cache_note=None, last_success=None, fallback_iso=None):
    """Финальная строка статуса: причина + пометка кэша + «Обновлено: HH:MM».

    Время успеха добавляется только если оно есть; иначе статус остаётся
    без него (например, «Нет интернета. Нет данных.»).
    """
    parts = []
    if reason:
        parts.append(reason)
    if cache_note:
        parts.append(cache_note)
    updated = updated_status_text(last_success, fallback_iso)
    if not updated.endswith("--:--"):
        parts.append(updated)
    if not parts:
        return updated
    return " ".join(parts)


def success_moment(weather):
    """Момент УСПЕШНОГО fetch: unix сейчас + offset timezone места из ответа."""
    try:
        offset = int(weather.get("utc_offset_seconds") or 0)
    except Exception:
        offset = 0
    return {"unix": int(time.time()), "offset": offset}


def record_success_unix(record):
    """unix последнего успеха из кэш-записи (None, если запись старого формата)."""
    if not isinstance(record, dict):
        return None
    ls = record.get("last_success")
    if isinstance(ls, dict) and ls.get("unix") is not None:
        try:
            return int(ls["unix"])
        except Exception:
            return None
    return None


def pick_newer_success(current, incoming):
    """Не даёт более старому моменту подменить более новый (и наоборот)."""
    cur = current.get("unix") if isinstance(current, dict) else None
    inc = incoming.get("unix") if isinstance(incoming, dict) else None
    if inc is None:
        return current
    if cur is None:
        return incoming
    return incoming if int(inc) >= int(cur) else current


def record_newer_than_success(record, current_success):
    """Правило write-if-newer/read-if-newer: кэш-файл новее показанного?"""
    inc = record_success_unix(record)
    if inc is None:
        return False
    cur = current_success.get("unix") if isinstance(current_success, dict) else None
    if cur is None:
        return True
    return int(inc) > int(cur)


def bg_valid_coords(coords):
    """Последние ПОДТВЕРЖДЁННЫЕ координаты: (lat, lon), либо None.

    Фоновому обновлению нужен только кэш-файл — GPS в фоне НЕ запрашивается
    (нет разрешений на фоновый геолокации и не тратим батарею).
    """
    if not isinstance(coords, dict):
        return None
    try:
        lat = float(coords.get("lat"))
        lon = float(coords.get("lon"))
    except (TypeError, ValueError):
        return None
    # NaN не проходит проверки сравнений, Inf — тоже.
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


def apply_weather_record(path, weather):
    """Write-if-newer запись нормализованной погоды в кэш-файл.

    Общая часть фонового пути: её делают и run_background_update (сетевой
    fetch в Python), и consume_bg_raw (приём сырого ответа Java-воркера).
    Любая ошибка/отказ -> False, файл байт-в-байт НЕ меняется (прежние
    данные и прежнее «Обновлено» остаются).
    """
    if not isinstance(weather, dict) or not weather.get("daily"):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception:
        return False
    if not isinstance(record, dict):
        return False
    moment = success_moment(weather)
    ex_unix = record_success_unix(record)
    if ex_unix is not None and int(ex_unix) > int(moment["unix"]):
        # Файл новее нашего момента (перевод часов) — write-if-newer, не пишем.
        return False
    old_ls = record.get("last_success")
    if not isinstance(old_ls, dict):
        old_ls = None
    coords_full = record.get("coords")
    new_record = {
        "coords": coords_full if isinstance(coords_full, dict) else {},
        "location_name": record.get("location_name") or "",
        "weather": weather,
        "last_success": pick_newer_success(old_ls, moment),
        "saved_at": datetime.now().strftime("%d.%m %H:%M"),
    }
    # Атомарная запись: temp-файл + os.replace (читатель не видит полфайла).
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new_record, f, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
    return True


def run_background_update(path=None, fetch=None):
    """Фоновое обновление погоды: без UI, без GPS, атомарная запись кэша.

    Берёт последние подтверждённые координаты из кэш-файла, запрашивает
    Open-Meteo и атомарно перезаписывает файл (write-if-newer: более новый
    last_success не откатывается, город из кэша не трогаем). Успех -> True;
    любой отказ (нет файла/координат/сети/валидного ответа) -> False, файл
    НЕ меняется — время «Обновлено» в статусе не сдвигается без успеха.

    Диагностика: начало/конец печатаются с epoch — по логам видно, что и
    когда выполнялось.
    """
    path = path or CACHE_FILE
    fetch = fetch or net_get_json
    t0 = time.time()
    print(f"[BG] update start epoch={int(t0)} path={path}", flush=True)
    try:
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except Exception:
        print(f"[BG] update end ok=False reason=no-cache dur_ms="
              f"{int((time.time() - t0) * 1000)}", flush=True)
        return False
    if not isinstance(record, dict):
        print(f"[BG] update end ok=False reason=bad-record dur_ms="
              f"{int((time.time() - t0) * 1000)}", flush=True)
        return False
    coords = bg_valid_coords(record.get("coords"))
    if coords is None:
        print(f"[BG] update end ok=False reason=no-coords dur_ms="
              f"{int((time.time() - t0) * 1000)}", flush=True)
        return False
    lat, lon = coords
    url = OPEN_METEO_URL.format(lat=lat, lon=lon)
    try:
        _status, data = fetch(url, timeout=20.0)
    except Exception:
        print(f"[BG] update end ok=False reason=fetch-failed dur_ms="
              f"{int((time.time() - t0) * 1000)}", flush=True)
        return False
    weather = normalize_weather(data)
    ok = apply_weather_record(path, weather)
    print(f"[BG] update end ok={ok} dur_ms={int((time.time() - t0) * 1000)}",
          flush=True)
    return ok


def consume_bg_raw(path=None):
    """Принимает сырой ответ Open-Meteo, сохранённый фоновым Java-воркером.

    WorkManager пишет wildvantage_bg_raw.json рядом с кэшем (атомарно);
    здесь — normalize_weather + та же write-if-newer запись, что и у
    run_background_update, только без сетевого запроса. Имя raw-файла
    забирается атомарно (os.replace в *.proc), чтобы параллельный fetch
    воркера не был съеден. Отказ/битый файл -> False, кэш не меняется.
    """
    path = path or CACHE_FILE
    base = os.path.dirname(os.path.abspath(path))
    raw_path = os.path.join(base, BG_RAW_FILE)
    proc = raw_path + ".proc"
    t0 = time.time()
    try:
        os.replace(raw_path, proc)
    except OSError:
        return False  # raw-файла нет — нечего принимать
    try:
        try:
            with open(proc, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            print(f"[BG] consume end ok=False reason=bad-raw epoch={int(time.time())}",
                  flush=True)
            return False
        weather = normalize_weather(data)
        ok = apply_weather_record(path, weather)
        print(f"[BG] consume end ok={ok} epoch={int(time.time())} dur_ms="
              f"{int((time.time() - t0) * 1000)}", flush=True)
        return ok
    finally:
        try:
            if os.path.exists(proc):
                os.remove(proc)
        except Exception:
            pass


def fmt_ddmm(iso):
    if isinstance(iso, str) and len(iso) >= 10 and iso[4] == "-" and iso[7] == "-":
        return f"{iso[8:10]}.{iso[5:7]}"
    return "--.--"


def humidity_text(value):
    try:
        return f"{float(value):.0f}%"
    except Exception:
        return "--%"


def wind_text(value):
    try:
        return f"{float(value):.0f} м/с"
    except Exception:
        return "-- м/с"


def slice_hours(hours, current_iso, count=24):
    """Строгий срез UI-ленты: текущий локальный час + следующие count-1 часов.

    Ровно count элементов, когда данных хватает: первый — час «сейчас»
    (2026-10-04T09 для «сейчас 09:37»), последний — сейчас + (count-1) ч,
    переход через полночь поддержан. Прошедшие часы в срез не попадают.
    Нет текущего часа в данных — берём первый будущий; всё устарело —
    начинаем с начала (показать имеющееся лучше, чем пустоту).
    """
    if not hours:
        return []
    start = 0
    if isinstance(current_iso, str) and len(current_iso) >= 13 and "T" in current_iso:
        cur_key = current_iso[:13]  # YYYY-MM-DDTHH
        exact = None
        future = None
        for i, h in enumerate(hours):
            t = h.get("time")
            if not isinstance(t, str) or len(t) < 13:
                continue
            k = t[:13]
            if exact is None and k == cur_key:
                exact = i
            if future is None and k > cur_key:
                future = i
        if exact is not None:
            start = exact
        elif future is not None:
            start = future
        else:
            start = 0
    return hours[start:start + count]


KV = """
MDScreen:
    md_bg_color: 0.05, 0.08, 0.05, 1

    MDScrollView:
        id: main_scroll
        do_scroll_x: False
        bar_width: "4dp"
        MDBoxLayout:
            orientation: 'vertical'
            size_hint_y: None
            height: self.minimum_height
            padding: ["12dp", "12dp", "12dp", "12dp"]
            spacing: "10dp"

            # Отступ под системные insets (status bar сверху / nav bar снизу).
            # Высота ставится из Android API в on_start (0 на десктопе).
            MDBoxLayout:
                id: header_spacer
                size_hint_y: None
                height: "0dp"

            MDLabel:
                text: "WILDVANTAGE"
                halign: "center"
                bold: True
                size_hint_y: None
                height: max(dp(40), self.texture_size[1] + dp(6))
                font_size: "22sp"
                theme_text_color: "Custom"
                text_color: 0.6, 0.9, 0.6, 1

            # Поиск города
            MDBoxLayout:
                orientation: 'horizontal'
                size_hint_y: None
                height: "48dp"
                spacing: "6dp"
                MDTextField:
                    id: city_input
                    hint_text: "Введите город"
                    mode: "round"
                    size_hint_y: None
                    height: "48dp"
                    helper_text_mode: "on_error"
                    on_text_validate: app.search_logic()
                MDIconButton:
                    icon: "magnify"
                    size_hint_y: None
                    size_hint_x: None
                    size: "48dp", "48dp"
                    on_release: app.search_logic()

            # Переключатель режима (фиксированная высота — не зависит от
            # minimum_height: children с size_hint_y дают в minimum 0)
            MDBoxLayout:
                orientation: 'horizontal'
                size_hint_y: None
                height: "48dp"
                spacing: "8dp"
                MDLabel:
                    id: mode_text
                    text: "РЕЖИМ ГОРОД (ONLINE)"
                    bold: True
                    theme_text_color: "Custom"
                    text_color: 0.85, 1, 0.85, 1
                MDSwitch:
                    id: mode_switch
                    active: False
                    on_active: app.toggle_mode(*args)

            # Карточка «Сейчас»
            MDCard:
                orientation: 'vertical'
                size_hint_y: None
                height: self.minimum_height
                padding: ["16dp", "14dp", "16dp", "14dp"]
                spacing: "8dp"
                radius: 18
                elevation: 0
                md_bg_color: 0.1, 0.15, 0.1, 1
                line_color: 0.2, 0.4, 0.2, 1

                MDLabel:
                    id: loc_label
                    text: "Ожидание данных..."
                    halign: "center"
                    font_style: "H6"
                    bold: True
                    shorten: True
                    shorten_from: "center"
                    size_hint_y: None
                    height: max(dp(30), self.texture_size[1] + dp(6))
                    valign: "middle"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: 0.9, 1, 0.9, 1

                MDLabel:
                    id: coords_label
                    text: "--"
                    halign: "center"
                    bold: True
                    font_size: "15sp"
                    shorten: True
                    size_hint_y: None
                    height: max(dp(22), self.texture_size[1] + dp(6))
                    valign: "middle"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: 0.7, 0.9, 0.7, 1

                MDLabel:
                    id: main_temp
                    text: "--°"
                    halign: "center"
                    font_size: "52sp"
                    bold: True
                    size_hint_y: None
                    height: max(dp(66), self.texture_size[1] + dp(6))
                    valign: "middle"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: 0.95, 1, 0.95, 1

                # Состояние: иконка + описание (сразу после температуры)
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: max(dp(24), self.minimum_height)
                    MDBoxLayout:
                        size_hint_x: 1
                    MDBoxLayout:
                        size_hint: None, None
                        width: self.minimum_width
                        height: self.minimum_height
                        MDIcon:
                            id: current_icon
                            icon: "update"
                            font_size: "22sp"
                            size_hint: None, None
                            size: "22dp", "22dp"
                            theme_text_color: "Custom"
                            text_color: 0.95, 1, 0.95, 1
                        MDLabel:
                            id: current_desc_label
                            text: "Загрузка..."
                            font_size: "16sp"
                            size_hint: None, None
                            width: "225dp"
                            height: max(dp(24), self.texture_size[1] + dp(4))
                            shorten: True
                            text_size: self.width, None
                            halign: "center"
                            valign: "middle"
                            theme_text_color: "Custom"
                            text_color: 0.85, 1, 0.85, 1
                    MDBoxLayout:
                        size_hint_x: 1

                # Детали «сейчас»: ощущается | влажность | ветер
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: max(dp(38), self.minimum_height)
                    MDFloatLayout:
                        size_hint_x: 1
                        MDBoxLayout:
                            size_hint: None, None
                            width: self.minimum_width
                            height: self.minimum_height
                            pos_hint: {"center_x": .5, "center_y": .5}
                            MDIcon:
                                icon: "thermometer"
                                font_size: "18sp"
                                size_hint: None, None
                                size: "18dp", "18dp"
                                theme_text_color: "Custom"
                                text_color: 0.95, 0.85, 0.45, 1
                            MDBoxLayout:
                                orientation: "vertical"
                                size_hint: None, None
                                width: "66dp"
                                height: self.minimum_height
                                spacing: "1dp"
                                MDLabel:
                                    text: "Ощущается"
                                    font_size: "10sp"
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(13), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.6, 0.75, 0.6, 1
                                MDLabel:
                                    id: feels_label
                                    text: "--°"
                                    font_size: "14sp"
                                    bold: True
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(20), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.85, 1, 0.85, 1
                    MDFloatLayout:
                        size_hint_x: 1
                        MDBoxLayout:
                            size_hint: None, None
                            width: self.minimum_width
                            height: self.minimum_height
                            pos_hint: {"center_x": .5, "center_y": .5}
                            MDIcon:
                                icon: "water-percent"
                                font_size: "18sp"
                                size_hint: None, None
                                size: "18dp", "18dp"
                                theme_text_color: "Custom"
                                text_color: 0.45, 0.7, 0.95, 1
                            MDBoxLayout:
                                orientation: "vertical"
                                size_hint: None, None
                                width: "66dp"
                                height: self.minimum_height
                                spacing: "1dp"
                                MDLabel:
                                    text: "Влажность"
                                    font_size: "10sp"
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(13), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.6, 0.75, 0.6, 1
                                MDLabel:
                                    id: humidity_label
                                    text: "--%"
                                    font_size: "14sp"
                                    bold: True
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(20), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.85, 1, 0.85, 1
                    MDFloatLayout:
                        size_hint_x: 1
                        MDBoxLayout:
                            size_hint: None, None
                            width: self.minimum_width
                            height: self.minimum_height
                            pos_hint: {"center_x": .5, "center_y": .5}
                            MDIcon:
                                icon: "weather-windy"
                                font_size: "18sp"
                                size_hint: None, None
                                size: "18dp", "18dp"
                                theme_text_color: "Custom"
                                text_color: 0.6, 0.85, 0.6, 1
                            MDBoxLayout:
                                orientation: "vertical"
                                size_hint: None, None
                                width: "66dp"
                                height: self.minimum_height
                                spacing: "1dp"
                                MDLabel:
                                    text: "Ветер"
                                    font_size: "10sp"
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(13), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.6, 0.75, 0.6, 1
                                MDLabel:
                                    id: wind_label
                                    text: "-- м/с"
                                    font_size: "14sp"
                                    bold: True
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(20), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.85, 1, 0.85, 1

                # Восход / Закат: подпись + время (время в отдельной строке — не разрывается)
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: max(dp(38), self.minimum_height)
                    MDFloatLayout:
                        size_hint_x: 1
                        MDBoxLayout:
                            size_hint: None, None
                            width: self.minimum_width
                            height: self.minimum_height
                            pos_hint: {"center_x": .5, "center_y": .5}
                            MDIcon:
                                icon: "weather-sunset-up"
                                font_size: "22sp"
                                size_hint: None, None
                                size: "18dp", "18dp"
                                theme_text_color: "Custom"
                                text_color: 0.95, 0.85, 0.45, 1
                            MDBoxLayout:
                                orientation: "vertical"
                                size_hint: None, None
                                width: "66dp"
                                height: self.minimum_height
                                spacing: "1dp"
                                MDLabel:
                                    text: "Восход"
                                    font_size: "10sp"
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(13), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.6, 0.75, 0.6, 1
                                MDLabel:
                                    id: sunrise_label
                                    text: "--:--"
                                    font_size: "14sp"
                                    bold: True
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(20), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.85, 1, 0.85, 1
                    MDFloatLayout:
                        size_hint_x: 1
                        MDBoxLayout:
                            size_hint: None, None
                            width: self.minimum_width
                            height: self.minimum_height
                            pos_hint: {"center_x": .5, "center_y": .5}
                            MDIcon:
                                icon: "weather-sunset-down"
                                font_size: "22sp"
                                size_hint: None, None
                                size: "18dp", "18dp"
                                theme_text_color: "Custom"
                                text_color: 0.95, 0.6, 0.4, 1
                            MDBoxLayout:
                                orientation: "vertical"
                                size_hint: None, None
                                width: "66dp"
                                height: self.minimum_height
                                spacing: "1dp"
                                MDLabel:
                                    text: "Закат"
                                    font_size: "10sp"
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(13), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.6, 0.75, 0.6, 1
                                MDLabel:
                                    id: sunset_label
                                    text: "--:--"
                                    font_size: "14sp"
                                    bold: True
                                    halign: "center"
                                    shorten: True
                                    size_hint: None, None
                                    width: "66dp"
                                    height: max(dp(20), self.texture_size[1] + dp(2))
                                    text_size: self.width, None
                                    theme_text_color: "Custom"
                                    text_color: 0.85, 1, 0.85, 1

                # Статус обновления (погода/сеть/кэш)
                MDLabel:
                    id: status_label
                    text: "Обновлено: --:--"
                    halign: "center"
                    valign: "middle"
                    font_size: "13sp"
                    shorten: True
                    size_hint_y: None
                    height: max(dp(20), self.texture_size[1] + dp(6))
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: 0.55, 0.75, 0.55, 1

                # GPS-статус — свой блок внутри карточки (не на поиске/заголовке)
                MDLabel:
                    id: gps_status_label
                    text: ""
                    halign: "center"
                    valign: "middle"
                    font_size: "13sp"
                    shorten: True
                    size_hint_y: None
                    height: max(dp(18), self.texture_size[1] + dp(4))
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: 0.65, 0.85, 0.65, 1

                # Кнопка Обновить GPS (центрируется по ширине).
                # Высота строки фиксирована: minimum_height зависел бы от
                # адаптивной высоты KivyMD-кнопки (её class-rule пересчитывает
                # width/height из texture_size и перетирает наши значения).
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: "48dp"
                    MDBoxLayout:
                        size_hint_x: 1
                    MDFillRoundFlatButton:
                        text: "ОБНОВИТЬ GPS"
                        # KivyMD пересчитывает width/height кнопки из
                        # texture_size и перетирает статические значения —
                        # ставим пол через родные _min_width/_min_height:
                        # результат детерминирован и не зависит от шрифта.
                        _min_width: "240dp"
                        _min_height: "44dp"
                        size_hint: None, None
                        pos_hint: {"center_y": .5}
                        md_bg_color: 0.2, 0.4, 0.2, 1
                        on_release: app.run_gps_logic()
                    MDBoxLayout:
                        size_hint_x: 1

            # Почасовая погода — ровно 24 карточки: текущий час + следующие 23 часа
            MDLabel:
                id: hourly_label
                text: "ПОЧАСОВАЯ ПОГОДА"
                bold: True
                size_hint_y: None
                height: max(dp(20), self.texture_size[1] + dp(6))
                theme_text_color: "Custom"
                text_color: 0.6, 0.9, 0.6, 1

            MDScrollView:
                id: hourly_scroll
                size_hint_y: None
                # Запас под адаптивную высоту ленты (92) + граница ячейки
                height: "96dp"
                do_scroll_x: True
                do_scroll_y: False
                bar_width: "3dp"
                MDBoxLayout:
                    id: hourly_row
                    orientation: 'horizontal'
                    size_hint_x: None
                    width: self.minimum_width
                    size_hint_y: None
                    height: max(dp(88), self.minimum_height)
                    spacing: "6dp"

            MDLabel:
                text: "ПРОГНОЗ НА 7 ДНЕЙ"
                bold: True
                size_hint_y: None
                height: max(dp(20), self.texture_size[1] + dp(6))
                theme_text_color: "Custom"
                text_color: 0.6, 0.9, 0.6, 1

            MDList:
                id: forecast_list
                size_hint_y: None
                height: self.minimum_height
                spacing: "6dp"

            MDBoxLayout:
                id: bottom_spacer
                size_hint_y: None
                height: "0dp"
"""


class WildVantage(MDApp):
    def build(self):
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Green"
        self.theme_cls.primary_hue = "900"
        self.cache_file = CACHE_FILE
        self.is_wilderness = False
        self._gen = 0
        self._last_weather = None
        self._last_coords = None
        self._last_success = None   # {"unix", "offset"} — последний УСПЕШНЫЙ fetch
        self._fetch_inflight = False  # идёт живой запрос погоды (синк кэша ждёт)
        self._loc_name = ""
        self._loc_name_coords = ""    # ключ координат владельца текущего названия
        self._reset_gps_state()
        return Builder.load_string(KV)

    def on_start(self):
        if platform == "android":
            self._request_permissions()
            self._schedule_bg_updates()
        self._set_safe_areas()
        self._start_gen = self._gen
        Clock.schedule_once(self._safe_start, 2)
        # Подхват кэша, пока приложение открыто: фоновый Java-воркер
        # (WorkManager, ~15 мин) пишет сырой ответ — мы принимаем его в кэш
        # и обновляем показ без закрытия приложения. Это НЕ механизм
        # периодического запроса (его делает воркер), а лишь синхронизация
        # UI с уже записанным результатом.
        Clock.schedule_interval(self._maybe_sync_from_cache, 60)

    def on_resume(self):
        # Вернулись из фона: если фон успел записать более свежий кэш —
        # показываем его сразу (нет in-flight запроса и нет GPS-сессии).
        self._maybe_sync_from_cache()

    def _maybe_sync_from_cache(self, *args):
        """Синхронизация UI с кэшем: читаем только более свежие данные."""
        if getattr(self, "_fetch_inflight", False) or getattr(self, "_gps_active", False):
            return
        # Сначала принимаем сырой ответ фонового Java-воркера (без сети —
        # файл уже записан): кэш обновляется, затем обычный синк его покажет.
        try:
            if consume_bg_raw(self.cache_file):
                self.log("sync: принят сырой ответ фонового воркера")
        except Exception as e:
            self._log_exc("consume bg raw (sync)", e)
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                record = json.load(f)
        except Exception:
            return
        if not record_newer_than_success(record, getattr(self, "_last_success", None)):
            return
        self.log("sync: кэш новее показанного — перечитываем")
        try:
            self.load_cache(gen=self._gen)
        except Exception as e:
            self._log_exc("sync load_cache", e)

    def _android_insets(self):
        """Верхний/нижний системные инсеты в px (status/nav bar).

        На Android читаем реальный RootWindowInsets через pyjnius; если не
        удалось (например, десктоп) — возвращаем (0, 0).
        """
        try:
            from jnius import autoclass

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            activity = PythonActivity.mActivity
            decor = activity.getWindow().getDecorView()
            insets = decor.getRootWindowInsets()
            top = 0
            bottom = 0
            if insets is not None:
                top = int(insets.getSystemWindowInsetTop() or 0)
                bottom = int(insets.getSystemWindowInsetBottom() or 0)
            self.log("android insets (px): top=", top, "bottom=", bottom)
            return top, bottom
        except Exception:
            return 0, 0

    def _set_safe_areas(self):
        """Ставит реальные отступы под status bar / nav bar в scroll-контент."""
        try:
            top, bottom = self._android_insets()
            root = self.root.ids
            root.header_spacer.height = float(top)
            root.bottom_spacer.height = float(bottom)
        except Exception as e:
            self._log_exc("safe areas", e)

    def _request_permissions(self):
        try:
            from android.permissions import request_permissions, Permission

            request_permissions(
                [
                    Permission.ACCESS_FINE_LOCATION,
                    Permission.ACCESS_COARSE_LOCATION,
                    Permission.INTERNET,
                ]
            )
        except Exception:
            pass

    def _schedule_bg_updates(self):
        """Ставит периодическое фоновое обновление (WorkManager, ~15 мин).

        Вся работа с WorkManager — в Java-классе BgScheduler (путь API
        проверяется компилятором при сборке), здесь только один вызов через
        pyjnius. Повторные запуски идемпотентны (unique work + KEEP). Если
        что-то недоступно — логируем и идём дальше: при открытом приложении
        запросы работают независимо от фонового расписания.
        """
        try:
            from jnius import autoclass

            PythonActivity = autoclass("org.kivy.android.PythonActivity")
            BgScheduler = autoclass("org.wildvantage.BgScheduler")
            ok = BgScheduler.schedule(PythonActivity.mActivity)
            self.log("bg update scheduled:", ok)
            return bool(ok)
        except Exception as e:
            self._log_exc("schedule bg update", e)
            return False

    def _safe_start(self, *args):
        # Миграция кэша: старые файлы с прежним названием («Косино» и им подобным)
        # удаляем, чтобы название не переживало эту версию.
        for old in ("wildvantage_v2.json", "wildvantage_v3.json"):
            try:
                if os.path.exists(old):
                    os.remove(old)
                    self.log("cache: удалён старый файл", old)
            except Exception:
                pass
        # Ошибка обработки кэша не должна закрывать приложение на старте (~2 с).
        # Сначала принимаем сырой ответ фонового воркера (если он успел
        # прийти, пока приложение было закрыто) — затем показываем кэш.
        try:
            if consume_bg_raw(self.cache_file):
                self.log("cache: принят сырой ответ фонового воркера")
        except Exception as e:
            self._log_exc("consume bg raw (start)", e)
        # Кэш НЕ подменяет свежие данные: если за 2 с уже пришёл (или идёт)
        # живой запрос — не трогаем показ.
        try:
            start_gen = getattr(self, "_start_gen", None)
            if self._last_weather is None and (start_gen is None or start_gen == self._gen):
                self.load_cache(gen=start_gen)
        except Exception as e:
            self._log_exc("load_cache", e)
            self._set_status("Кэш не читается — включите интернет")

    def log(self, *args):
        try:
            print("[WildVantage]", *args)
        except Exception:
            pass

    def _log_exc(self, where, exc):
        """Полный traceback неожиданного исключения в лог, процесс не роняем."""
        try:
            import traceback
            self.log(f"crash-защита: {where}: {type(exc).__name__}: {exc}")
            for line in traceback.format_exc().splitlines():
                self.log("  ", line)
        except Exception:
            pass

    def _set_status(self, text):
        try:
            self.root.ids.status_label.text = text
        except Exception:
            pass

    def _set_gps_status(self, text):
        """GPS-статус — в собственном блоке карточки (gps_status_label).

        Никогда не пишется в строку поиска/заголовок: у него свой id,
        фиксированный вертикальным потоком между статусом погоды и
        кнопкой ОБНОВИТЬ GPS.
        """
        try:
            self.root.ids.gps_status_label.text = text or ""
        except Exception:
            pass

    # --- ПЕРЕКЛЮЧЕНИЕ РЕЖИМА ---
    def toggle_mode(self, instance, value):
        self.is_wilderness = value
        try:
            root = self.root.ids
            if value:
                root.mode_text.text = "РЕЖИМ ГЛУШИ (OFFLINE)"
                root.mode_text.text_color = 1, 0.4, 0.4, 1
            else:
                root.mode_text.text = "РЕЖИМ ГОРОД (ONLINE)"
                root.mode_text.text_color = 0.85, 1, 0.85, 1
        except Exception:
            pass

    def _set_coords_text(self, lat, lon, accuracy):
        text = f"{lat:.5f}, {lon:.5f}"
        try:
            if accuracy is not None:
                text += f"  ±{float(accuracy):.0f} м"
        except Exception:
            pass
        try:
            self.root.ids.coords_label.text = text
        except Exception:
            pass

    def _set_location_name(self, name):
        self._loc_name = name or ""
        try:
            self.root.ids.loc_label.text = name or "Точка GPS"
        except Exception:
            pass

    def _name_matches_coords(self, lat, lon):
        """Текущее название принадлежит ИМЕННО этим координатам?"""
        return bool(getattr(self, "_loc_name", "")) and (
            getattr(self, "_loc_name_coords", "") == f"{lat:.5f}, {lon:.5f}"
        )

    def _set_name_coords_key(self, lat, lon):
        self._loc_name_coords = f"{lat:.5f}, {lon:.5f}"

    # --- АСИНХРОННЫЙ HTTP (Kivy на Android) ---
    def _fetch(self, url, on_success=None, on_error=None, timeout=None, headers=None,
               resolver=None, opener=None):
        """GET в фоновом потоке (сетевой код не блокирует UI), результат
        доставляется в главный поток через Clock.schedule_once.

        Ошибка колбэку — всегда NetError: kind (dns/timeout/connect/tls/http/
        json), host (только hostname, без секретов), online — результат пробы
        сети (None, если проба не требовалась). Каждый колбэк вызывается ровно
        один раз.
        """

        def deliver_success(payload):
            def _go(_dt):
                if on_success is None:
                    return
                try:
                    on_success(None, payload)
                except Exception as e:
                    self._log_exc("network callback", e)
            Clock.schedule_once(_go)

        def deliver_error(err):
            def _go(_dt):
                if on_error is None:
                    return
                try:
                    on_error(None, err)
                except Exception as e:
                    self._log_exc("network callback", e)
            Clock.schedule_once(_go)

        def worker():
            host = urllib.parse.urlsplit(url).hostname or ""
            try:
                status, body = net_get(
                    url,
                    timeout=float(timeout) if timeout else 12.0,
                    headers=headers,
                    resolver=resolver,
                    opener=opener,
                )
            except NetError as e:
                if e.kind in ("http", "json"):
                    # Сервис ответил — интернет есть, проба не нужна.
                    e.online = True
                else:
                    e.online = probe_online()
                deliver_error(e)
                return
            except Exception as e:
                err = NetError("unknown", host, f"{type(e).__name__}: {e}")
                err.online = probe_online()
                deliver_error(err)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception as e:
                err = NetError("json", host, f"{type(e).__name__}: {e}")
                err.online = True
                deliver_error(err)
                return
            deliver_success(payload)

        threading.Thread(target=worker, daemon=True).start()

    # --- ПОИСК ГОРОДА ---
    def search_logic(self):
        city = self.root.ids.city_input.text.strip()
        if not city:
            self._set_status("Введите город")
            return
        if self.is_wilderness:
            self.load_cache(city_filter=city)
            return
        self._set_status("Поиск города...")
        url = GEOCODE_URL.format(q=urllib.parse.quote(city))
        self._fetch(url, on_success=self.on_geocode_success, on_error=self.on_geocode_error, timeout=10)

    def on_geocode_success(self, _req, result):
        results = result.get("results") if isinstance(result, dict) else None
        if not results:
            self._set_status("Город не найден")
            return
        top = results[0]
        try:
            lat = float(top["latitude"])
            lon = float(top["longitude"])
        except Exception:
            self._set_status("Город не найден")
            return
        name = top.get("name") or "Населённый пункт"
        admin = top.get("admin1")
        if admin and admin.lower() != name.lower():
            name = f"{name}, {admin}"
        self.log("geocode:", name, lat, lon)
        self.start_update(lat, lon, source="search", display_name=name, timeout=15)

    def on_geocode_error(self, _req, err):
        self.log("geocode error:", err)
        self._set_status(net_error_text(err, "geocode"))

    # --- GPS: сбор нескольких fixes, выбор лучшего по accuracy ---
    def _reset_gps_state(self):
        # Полное прекращение предыдущего сеанса: стоп провайдера, отмена таймеров,
        # сброс накопленных fix. Повторное нажатие не должно давать двух слушателей.
        self._stop_gps()
        self._cancel_gps_timers()
        self._gps_fixes = []
        self._gps_best = None
        self._gps_last_ts = 0.0
        self._gps_active = False

    def _cancel_gps_timers(self):
        for attr in ("_gps_deadline", "_gps_settle"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
                setattr(self, attr, None)

    def _has_fine_location(self):
        """Проверка runtime-разрешения точного местоположения (Android)."""
        if platform != "android":
            return True
        try:
            from android.permissions import check_permission, Permission

            return bool(check_permission(Permission.ACCESS_FINE_LOCATION))
        except Exception:
            return True

    def run_gps_logic(self):
        self._set_gps_status("Поиск спутников…")
        if gps is None:
            self._set_gps_status("GPS недоступен")
            return
        if not self._has_fine_location():
            self._request_permissions()
            self._set_gps_status("Нужно разрешение на точное местоположение")
            return
        # Инвалидируем влетающие запросы предыдущего обновления (гонка):
        # их колбэки с gen != self._gen  будут отброшены.
        self._gen += 1
        self.log("gps: новый сеанс, gen =", self._gen)
        self._reset_gps_state()
        try:
            gps.configure(on_location=self.on_gps_loc, on_status=self.on_gps_status)
        except Exception:
            try:
                gps.configure(on_location=self.on_gps_loc)
            except Exception:
                self._set_gps_status("Ошибка GPS: включите спутники")
                return
        try:
            # частые обновления: было 1000 мс, теперь 500 мс
            gps.start(minTime=500, minDistance=0)
        except Exception:
            self._set_gps_status("Ошибка GPS: включите спутники")
            return
        self._gps_active = True
        self._gps_deadline = Clock.schedule_once(
            lambda dt: self._finish_gps("таймаут поиска"), GPS_SEARCH_TIMEOUT
        )
        self.log("gps: старт сбора fixes, лимит", GPS_SEARCH_TIMEOUT, "с")

    def _stop_gps(self):
        try:
            gps.stop()
        except Exception:
            pass

    def _arm_settle(self):
        """Ждём улучшения при уже приемлемой точности (<=20 м)."""
        if getattr(self, "_gps_settle", None) is not None:
            try:
                self._gps_settle.cancel()
            except Exception:
                pass
            self._gps_settle = None
        if self._gps_best and self._gps_best["accuracy"] <= GPS_OK_ACCURACY:
            self._gps_settle = Clock.schedule_once(
                lambda dt: self._finish_gps("нет улучшения при ≤20 м"), GPS_SETTLE_TIME
            )

    def on_gps_status(self, *args):
        # plyer вызывает on_status('provider-disabled'|'provider-status', value)
        # ПОЗИЦИОННО, поэтому принимаем *args, а не **kwargs.
        try:
            self.log("gps status:", args)
            if args and args[0] == "provider-disabled" and getattr(self, "_gps_active", False):
                self._set_gps_status("GPS выключен — включите спутники")
        except Exception as e:
            self._log_exc("on_gps_status", e)

    def on_gps_loc(self, **kwargs):
        try:
            self._on_gps_loc_impl(kwargs)
        except Exception as e:
            self._log_exc("on_gps_loc", e)

    def _on_gps_loc_impl(self, kwargs):
        if not self._gps_active:
            return
        now = time.time()
        try:
            lat = float(kwargs["lat"])
            lon = float(kwargs["lon"])
        except Exception:
            self.log("gps fix: отброшен — нет/некорректные координаты", kwargs)
            return

        acc_raw = kwargs.get("accuracy")
        try:
            accuracy = float(acc_raw) if acc_raw is not None else None
        except Exception:
            accuracy = None

        ts_raw = kwargs.get("timestamp")
        try:
            ts = float(ts_raw) if ts_raw is not None else now
        except Exception:
            ts = now

        self.log(
            "gps fix:",
            "lat=", lat,
            "lon=", lon,
            "accuracy=", accuracy,
            "timestamp=", ts,
            "arrived=", now,
        )

        # Отбрасываем явно устаревшие / некорректные измерения.
        if ts < self._gps_last_ts - 1.0:
            self.log("  -> отклонён: устаревший (out-of-order) fix")
            return
        if now - ts > GPS_STALE_AGE:
            self.log("  -> отклонён: fix старше", GPS_STALE_AGE, "с")
            return
        if accuracy is None or accuracy <= 0:
            self.log("  -> отклонён: нет корректной accuracy")
            return

        self._gps_last_ts = max(self._gps_last_ts, ts)
        candidate = {"lat": lat, "lon": lon, "accuracy": accuracy, "ts": ts}
        self._gps_fixes.append(candidate)

        if self._gps_best is None or accuracy < self._gps_best["accuracy"] - 0.05:
            self._gps_best = candidate
            self.log("  -> принят: новый лучший accuracy", accuracy, "м")
            self._arm_settle()
        else:
            self.log(
                "  -> отклонён: хуже текущего лучшего",
                self._gps_best["accuracy"], "м",
            )

        best_acc = self._gps_best["accuracy"]
        self._set_gps_status(f"Уточнение GPS… ±{best_acc:.0f} м")

        if best_acc <= GPS_GOOD_ACCURACY:
            self._finish_gps("точность ≤10 м")
        elif best_acc <= GPS_OK_ACCURACY:
            self._arm_settle()

    def _finish_gps(self, reason):
        if not self._gps_active:
            return
        self._cancel_gps_timers()
        self._gps_active = False
        self._stop_gps()
        best = self._gps_best
        if best is None:
            self.log("gps: завершение —", reason, "| валидных fix нет")
            self._set_gps_status("GPS: нет сигнала. Проверьте небо и разрешение.")
            return
        self.log(
            "gps: завершение —", reason,
            "| измерений:", len(self._gps_fixes),
            "| лучший ±", best["accuracy"], "м",
        )
        # Итог GPS — в свой блок (не в статус погоды).
        self._set_gps_status(f"GPS: ±{best['accuracy']:.0f} м")
        timeout = 3 if self.is_wilderness else 20
        self.start_update(
            best["lat"], best["lon"],
            accuracy=best["accuracy"],
            source="gps",
            timeout=timeout,
        )

    # --- ЗАПУСК ОБНОВЛЕНИЯ (единая точка, защита от гонок) ---
    def start_update(self, lat, lon, accuracy=None, source="gps", display_name=None, timeout=None):
        self._gen += 1
        gen = self._gen
        self._last_weather = None
        self._fetch_inflight = True
        self._last_coords = {"lat": lat, "lon": lon, "accuracy": accuracy}
        self._set_coords_text(lat, lon, accuracy)
        try:
            root = self.root.ids
            root.forecast_list.clear_widgets()
            root.hourly_row.clear_widgets()
            root.main_temp.text = "--°"
            root.feels_label.text = "--°"
            root.humidity_label.text = "--%"
            root.wind_label.text = "-- м/с"
            root.sunrise_label.text = "--:--"
            root.sunset_label.text = "--:--"
            root.current_desc_label.text = "Загрузка..."
            root.current_icon.icon = "update"
        except Exception:
            pass

        if display_name:
            self._set_location_name(display_name)
            self._set_name_coords_key(lat, lon)
        else:
            if not self._name_matches_coords(lat, lon):
                self._set_location_name(None)
                try:
                    self.root.ids.loc_label.text = "Определяю место..."
                except Exception:
                    pass
            # Те же координаты — прежнее название остаётся на экране, пока
            # Nominatim не уточнит его заново (защита «название пропало»).
            self.reverse_geocode(lat, lon, gen)

        self._set_status("Загрузка погоды...")
        self.fetch_weather(lat, lon, gen, timeout=timeout)

    # --- ОБРАТНОЕ ГЕОКОДИРОВАНИЕ (Nominatim/OSM) ---
    def reverse_geocode(self, lat, lon, gen):
        url = REVERSE_URL.format(lat=lat, lon=lon)
        headers = {"User-Agent": NOMINATIM_UA, "Accept-Language": "ru"}

        def success(_req, result):
            if gen != self._gen:
                return
            address = result.get("address") if isinstance(result, dict) else None
            details = location_pick_details(address)
            self.log("geocode: GPS coords =", lat, lon)
            self.log(
                "geocode: raw Nominatim address =",
                address if isinstance(address, dict) else None,
            )
            self.log(
                "geocode: selected field ->",
                (details.get("district_field"), details.get("district"))
                if details and details.get("district")
                else ("(город)", details.get("locality")),
            )
            name = details["name"] if details else None
            self.log("geocode: selected location name =", name)
            if name:
                self._set_location_name(name)
                self._set_name_coords_key(lat, lon)
                self._save_cache()
            else:
                self._set_location_name(f"{lat:.5f}, {lon:.5f}")
                self._set_name_coords_key(lat, lon, "coords")

        def error(_req, err):
            self.log("geocode: nominatim error:", err)
            if gen != self._gen:
                return
            # Название уже известно ДЛЯ ЭТИХ ЖЕ координат — не подменяем его
            # координатной строкой (баг «Косино пропало» при ошибке сети).
            if not self._name_matches_coords(lat, lon):
                self._set_location_name(f"{lat:.5f}, {lon:.5f}")
                self._set_name_coords_key(lat, lon)
            # Статус ошибки названия — только пока погода ещё не пришла:
            # свежие данные не затираются сообщением о геокодере.
            if self._last_weather is None:
                self._set_status(net_error_text(err, "nominatim"))

        self._fetch(url, on_success=success, on_error=error, timeout=12, headers=headers)

    # --- ПОГОДА (Open-Meteo) ---
    def fetch_weather(self, lat, lon, gen, timeout=None):
        url = OPEN_METEO_URL.format(lat=lat, lon=lon)
        self.log("weather request coords:", lat, lon)

        def success(_req, result):
            if gen != self._gen:
                return
            self._fetch_inflight = False
            weather = normalize_weather(result)
            if not weather or not weather.get("daily"):
                self.log("weather: пустой ответ")
                self.load_cache(
                    gen=gen,
                    show_err=True,
                    requested=self._last_coords,
                    status_msg="Сервис погоды недоступен",
                )
                return
            self._last_weather = weather
            # Последний УСПЕШНЫЙ fetch: unix сейчас + offset timezone места.
            self._last_success = pick_newer_success(
                getattr(self, "_last_success", None), success_moment(weather)
            )
            self.log("weather: источник = Open-Meteo (API)")
            self.log(
                "weather:",
                "tz=", weather.get("timezone"),
                "utc_offset=", weather.get("utc_offset_seconds"),
                "time=", weather["current"].get("time"),
                "temp=", weather["current"].get("temp"),
                "code=", weather["current"].get("code"),
                "cloud=", weather["current"].get("cloud_cover"),
                "is_day=", weather["current"].get("is_day"),
                "feels=", weather["current"].get("feels"),
                "humidity=", weather["current"].get("humidity"),
                "wind_speed=", weather["current"].get("wind_speed"),
            )
            self.log("weather: часовых значений =", len(weather.get("hours") or []))
            self.process_weather(weather, self._last_coords)
            self._save_cache()

        def error(_req, err):
            if gen != self._gen:
                return
            self._fetch_inflight = False
            self.log("weather error:", err)
            self.load_cache(
                gen=gen,
                show_err=True,
                requested=self._last_coords,
                status_msg=net_error_text(err, "weather"),
            )

        self._fetch(url, on_success=success, on_error=error, timeout=timeout)

    # --- ОТОБРАЖЕНИЕ ---
    def process_weather(self, weather, coords=None, from_cache=False):
        if not isinstance(weather, dict):
            self._set_status("Данные недоступны")
            return
        coords = coords if isinstance(coords, dict) else (self._last_coords or {})
        cur = weather.get("current") or {}
        days = weather.get("daily") or []
        is_day = True
        try:
            is_day = bool(int(cur.get("is_day", 1)))
        except Exception:
            pass
        desc, icon = wmo_info(cur.get("code"), is_day)
        self.log("display: is_day=", is_day, "desc=", desc, "icon=", icon)

        try:
            root = self.root.ids
            root.main_temp.text = fmt_temp(cur.get("temp"))
            root.current_desc_label.text = desc
            root.current_icon.icon = icon
            root.feels_label.text = fmt_temp(cur.get("feels"))
            root.humidity_label.text = humidity_text(cur.get("humidity"))
            root.wind_label.text = wind_text(cur.get("wind_speed"))
            if coords.get("lat") is not None and coords.get("lon") is not None:
                self._set_coords_text(coords["lat"], coords["lon"], coords.get("accuracy"))
            if self._loc_name:
                root.loc_label.text = self._loc_name
            root.forecast_list.clear_widgets()
        except Exception:
            pass

        self._fill_hours(
            slice_hours(weather.get("hours") or [], cur.get("time")),
            cur.get("time"),
        )

        if days:
            today = days[0]
            self.log("sunrise=", fmt_hhmm(today.get("sunrise")), "sunset=", fmt_hhmm(today.get("sunset")))
            try:
                self.root.ids.sunrise_label.text = fmt_hhmm(today.get("sunrise"))
                self.root.ids.sunset_label.text = fmt_hhmm(today.get("sunset"))
            except Exception:
                pass
            self._fill_forecast(days[:7])

        if not from_cache:
            # Для кэша финальный статус формирует load_cache (с причиной).
            # Время — только от последнего УСПЕШНОГО fetch (self._last_success).
            self._set_status(
                updated_status_text(getattr(self, "_last_success", None),
                                    cur.get("time"))
            )

    def _fill_hours(self, hours, current_time=None):
        """Строит ленту «ПОЧАСОВАЯ ПОГОДА»: ровно 24 карточки.

        Первый элемент — текущий локальный час (подписан «Сейчас»),
        далее следующие 23 часа (переход через полночь поддержан).
        Прошедшие часы в UI не отображаются; после обновления лента
        стоит в начале (scroll_x = 0) — автоскролл не нужен.
        """
        try:
            row = self.root.ids.hourly_row
            scroll = self.root.ids.hourly_scroll
        except Exception:
            return
        if current_time:
            hours = slice_hours(hours, current_time, count=24)
        else:
            hours = (list(hours) if hours else [])[:24]
        row.clear_widgets()
        for h in hours[:24]:
            try:
                self._build_hour_cell(row, h, current_time)
            except Exception as e:
                self._log_exc("hour cell", e)
        try:
            scroll.scroll_x = 0
        except Exception:
            pass

    def _build_hour_cell(self, row, h, current_time=None):
        time_iso = str(h.get("time") or "")
        now_iso = str(current_time or "")
        is_current = bool(now_iso) and len(time_iso) >= 13 and time_iso[:13] == now_iso[:13]
        is_past = bool(now_iso) and len(time_iso) >= 13 and len(now_iso) >= 13 and time_iso[:13] < now_iso[:13]
        if is_current:
            past = False
        else:
            past = is_past

        is_day = True
        try:
            is_day = bool(int(h.get("is_day", 1)))
        except Exception:
            pass
        _, icon = wmo_info(h.get("code"), is_day)
        box = MDBoxLayout(
            orientation="vertical",
            spacing="2dp",
            size_hint=(None, None),
            width=dp(52),
            height=dp(88),
        )
        # Ячейка адаптируется к содержимому (высоты строк защищены от
        # texture-размеров шрифта), но не меньше стандартных 88dp.
        box.bind(minimum_height=lambda inst, mh: setattr(
            inst, "height", max(dp(88), mh)))
        if is_current and Color is not None and RoundedRectangle is not None:
            # Подсветка текущего часа: скруглённая подложка (canvas, не pos_hint)
            try:
                with box.canvas.before:
                    Color(0.16, 0.30, 0.16, 1)
                    rect = RoundedRectangle(radius=[(6, 6, 6, 6)])
                    rect.pos = box.pos
                    rect.size = box.size
                box.bind(
                    pos=lambda inst, p: setattr(rect, "pos", p),
                    size=lambda inst, s: setattr(rect, "size", s),
                )
            except Exception as e:
                self._log_exc("hour highlight", e)

        if past:
            time_color = (0.5, 0.58, 0.5, 1)
            icon_color = (0.55, 0.6, 0.55, 1)
            temp_color = (0.6, 0.65, 0.6, 1)
        else:
            time_color = (0.7, 0.85, 0.7, 1)
            icon_color = (0.85, 1, 0.85, 1)
            temp_color = (0.95, 1, 0.95, 1)

        # Каждая строка ячейки: высота не меньше текстуры (+2dp запас) —
        # текст физически не вылезает за строку ни при каких метриках шрифта.
        def _guarded(w, base):
            w.height = base
            w.bind(texture_size=lambda inst, ts, b=base: setattr(
                inst, "height", max(b, ts[1] + dp(2))))
            return w

        # Всегда HH:MM — текущий час дополнительно выделен подписью «Сейчас»
        # и подсветкой (текст времени не заменяется).
        box.add_widget(_guarded(
            MDLabel(
                text=fmt_hhmm(h.get("time")),
                font_size="12sp",
                bold=is_current,
                halign="center",
                size_hint=(None, None),
                width=dp(52),
                theme_text_color="Custom",
                text_color=(0.85, 1, 0.85, 1) if is_current else time_color,
            ),
            dp(16),
        ))
        box.add_widget(_guarded(
            MDIcon(
                icon=icon,
                font_size="22sp",
                halign="center",
                theme_text_color="Custom",
                text_color=icon_color,
                size_hint=(None, None),
                width=dp(52),
            ),
            dp(26),
        ))
        box.add_widget(_guarded(
            MDLabel(
                text=fmt_temp(h.get("temp")),
                font_size="14sp",
                bold=True,
                halign="center",
                size_hint=(None, None),
                width=dp(52),
                theme_text_color="Custom",
                text_color=temp_color,
            ),
            dp(18),
        ))
        # Компактная подпись текущего часа (пустая у остальных — сохраняем
        # единый ритм ленты).
        box.add_widget(_guarded(
            MDLabel(
                text="Сейчас" if is_current else "",
                font_size="11sp",
                bold=is_current,
                halign="center",
                size_hint=(None, None),
                width=dp(52),
                theme_text_color="Custom",
                text_color=(0.6, 1, 0.6, 1) if is_current else (0, 0, 0, 0),
            ),
            dp(14),
        ))
        row.add_widget(box)
        return is_current

    def _fill_forecast(self, days):
        try:
            forecast_list = self.root.ids.forecast_list
        except Exception:
            return
        forecast_list.clear_widgets()
        for day in days:
            try:
                self._build_forecast_item(forecast_list, day)
            except Exception as e:
                self._log_exc("forecast item", e)

    def _build_forecast_item(self, forecast_list, day):
        code = day.get("code")
        d_desc, d_icon = wmo_info(code, True)
        text = f"{fmt_ddmm(day.get('date'))}  |  {fmt_temp(day.get('tmax'))} / {fmt_temp(day.get('tmin'))}"
        tertiary = "Осадки: --%"
        try:
            parts = []
            if day.get("precip") is not None:
                parts.append(f"Осадки: {float(day['precip']):.0f}%")
            if day.get("wind") is not None:
                parts.append(f"Ветер: {float(day['wind']):.0f} м/с")
            if parts:
                tertiary = " · ".join(parts)
        except Exception:
            pass
        item = ThreeLineIconListItem(
            text=text,
            secondary_text=d_desc,
            tertiary_text=tertiary,
        )
        item.add_widget(IconLeftWidget(icon=d_icon))
        forecast_list.add_widget(item)

    # --- КЭШ ---
    def _read_cache_record(self):
        """Кэш-запись или None. Ошибка чтения — не исключение."""
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                record = json.load(f)
            return record if isinstance(record, dict) else None
        except Exception:
            return None

    def _save_cache(self):
        if not self._last_weather or not self._last_coords:
            return
        record = {
            "coords": self._last_coords,
            "location_name": self._loc_name,
            "weather": self._last_weather,
            "last_success": getattr(self, "_last_success", None),
            "saved_at": datetime.now().strftime("%d.%m %H:%M"),
        }
        # Write-if-newer: более свежий файл (фоновое обновление) не затираем.
        existing = self._read_cache_record()
        if existing is not None:
            ex_unix = record_success_unix(existing)
            our_ls = getattr(self, "_last_success", None)
            our_unix = our_ls.get("unix") if isinstance(our_ls, dict) else None
            if ex_unix is not None and (our_unix is None or int(ex_unix) > int(our_unix)):
                self.log("cache: файл новее — запись пропущена", ex_unix, ">", our_unix)
                return
        # Атомарная запись: temp-файл + os.replace (читатель не увидит полфайла).
        tmp = self.cache_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, self.cache_file)
        except Exception:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def load_cache(self, gen=None, city_filter=None, show_err=False,
                   requested=None, status_msg=None):
        """Показать кэш. Статус всегда составляется здесь: причина (status_msg)
        + пометка, что данные из кэша + «Обновлено: HH:MM» последнего
        УСПЕШНОГО обновления (из кэша — это фактическое время фонового
        обновления, не время открытия). Свежий API приоритетнее кэша (gen-guard).
        Возвращает True, если данные кэша показаны.
        """
        if gen is not None and gen != self._gen:
            return False

        def fail(text):
            try:
                self._set_status(
                    compose_status(
                        text,
                        None,
                        getattr(self, "_last_success", None),
                        None,
                    )
                )
            except Exception:
                pass
            return False

        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                record = json.load(f)
        except Exception:
            if status_msg:
                return fail(f"{status_msg}. Нет данных.")
            return fail("Кэш пуст — включите интернет")

        if city_filter:
            name = record.get("location_name") or ""
            if city_filter.lower() not in name.lower():
                return fail("Город не найден в кэше")

        weather = record.get("weather")
        coords = record.get("coords") if isinstance(record.get("coords"), dict) else {}
        if not isinstance(weather, dict) or not weather.get("daily"):
            if status_msg:
                return fail(f"{status_msg}. Нет данных.")
            return fail("Кэш повреждён — включите интернет")

        # Время успеха: не даём более старой кэш-записи откатить более свежий
        # момент из памяти (ошибка API не меняет время последнего успеха).
        self._last_success = pick_newer_success(
            getattr(self, "_last_success", None), record.get("last_success")
        )
        fallback_iso = (weather.get("current") or {}).get("time")

        is_far = bool(requested) and self._coords_far(requested, coords)
        if is_far:
            # Кэш другого места: координаты/название не подменяем на чужие,
            # показываем текущую GPS-точку, а не прошлую локацию.
            coords = requested
        self.log("cache: чтение из файла | кэш другого места =", is_far)
        self._last_weather = weather
        self._last_coords = coords
        if is_far:
            self._set_location_name(f"{coords.get('lat', 0):.5f}, {coords.get('lon', 0):.5f}")
            self._loc_name_coords = f"{coords.get('lat', 0):.5f}, {coords.get('lon', 0):.5f}"
        else:
            self._set_location_name(record.get("location_name") or "Кэш")
            self._loc_name_coords = f"{coords.get('lat', 0):.5f}, {coords.get('lon', 0):.5f}"
        self.process_weather(weather, coords, from_cache=True)

        note = "Данные из кэша (другое место)." if is_far else "Данные из кэша."
        if status_msg:
            reason = status_msg
            if not reason.endswith((".", "!", "?")):
                reason += "."
            self._set_status(
                compose_status(reason, note, self._last_success, fallback_iso)
            )
            return True
        if is_far or show_err:
            self._set_status(
                compose_status(None, note, self._last_success, fallback_iso)
            )
        elif city_filter:
            self._set_status(
                compose_status(
                    "Офлайн: данные из кэша.", None, self._last_success, fallback_iso
                )
            )
        else:
            self._set_status(
                compose_status(None, note, self._last_success, fallback_iso)
            )
        return True

    def _coords_far(self, a, b):
        try:
            return abs(float(a["lat"]) - float(b["lat"])) > 0.05 or abs(
                float(a["lon"]) - float(b["lon"])
            ) > 0.05
        except Exception:
            return False


if __name__ == "__main__":
    WildVantage().run()
