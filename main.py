import json
import urllib.parse
from datetime import datetime

from kivy.clock import Clock
from kivy.utils import platform
from kivy.lang import Builder
from kivy.network.urlrequest import UrlRequest
from kivymd.app import MDApp
from kivymd.uix.list import ThreeLineIconListItem, IconLeftWidget

# GPS — отдельным try/except, чтобы сбой импорта не ронял приложение
try:
    from plyer import gps
except Exception:
    gps = None
try:
    import certifi
except Exception:
    certifi = None

CACHE_FILE = "wildvantage_v3.json"
NOMINATIM_UA = "WildVantage (Android weather app; contact: denikhorohenkov2025-hub)"
GPS_TIMEOUT = 25

# Погода: Open-Meteo (бесплатно, без ключа).
# current: temperature_2m, weather_code, cloud_cover, is_day
# daily:   weather_code, temperature_2m_max/min, sunrise, sunset
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,weather_code,cloud_cover,is_day"
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset"
    "&timezone=auto&forecast_days=6"
)
GEOCODE_URL = (
    "https://geocoding-api.open-meteo.com/v1/search"
    "?name={q}&count=1&language=ru&format=json"
)
REVERSE_URL = (
    "https://nominatim.openstreetmap.org/reverse"
    "?format=jsonv2&lat={lat}&lon={lon}&zoom=18&addressdetails=1&accept-language=ru"
)

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
    if not isinstance(address, dict):
        return None

    def first(keys):
        for k in keys:
            v = address.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return None

    district = _clean_part(
        first(
            [
                "neighbourhood",
                "suburb",
                "city_district",
                "quarter",
                "borough",
                "district",
                "city_block",
                "residential",
            ]
        )
    )
    locality = _clean_part(
        first(["city", "town", "village", "municipality", "hamlet", "locality"])
    )

    if district and locality and district.lower() != locality.lower():
        return f"{locality}, {district}"
    if district:
        return district
    if locality:
        return locality
    return _clean_part(first(["county", "state", "region"]))


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
            }
        )
    return {
        "current": {
            "time": cur.get("time"),
            "temp": cur.get("temperature_2m"),
            "code": cur.get("weather_code"),
            "cloud_cover": cur.get("cloud_cover"),
            "is_day": cur.get("is_day"),
        },
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


def fmt_ddmm(iso):
    if isinstance(iso, str) and len(iso) >= 10 and iso[4] == "-" and iso[7] == "-":
        return f"{iso[8:10]}.{iso[5:7]}"
    return "--.--"


KV = """
MDScreen:
    md_bg_color: 0.05, 0.08, 0.05, 1
    MDBoxLayout:
        orientation: 'vertical'
        padding: "12dp"
        spacing: "10dp"

        MDLabel:
            text: "WILDVANTAGE"
            halign: "center"
            bold: True
            size_hint_y: None
            height: "40dp"
            theme_text_color: "Custom"
            text_color: 0.6, 0.9, 0.6, 1

        # Этаж 1 — Поиск города
        MDBoxLayout:
            orientation: 'vertical'
            adaptive_height: True
            spacing: "8dp"
            MDBoxLayout:
                adaptive_height: True
                spacing: "5dp"
                MDTextField:
                    id: city_input
                    hint_text: "Введите город"
                    mode: "round"
                    input_type: "text"
                    helper_text_mode: "on_error"
                    on_text_validate: app.search_logic()
                MDIconButton:
                    icon: "magnify"
                    on_release: app.search_logic()

        # Этаж 2 — Переключатель режима
        MDBoxLayout:
            orientation: 'horizontal'
            adaptive_height: True
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

        MDCard:
            orientation: 'vertical'
            padding: "20dp"
            spacing: "9dp"
            size_hint_y: None
            height: "350dp"
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
                height: "28dp"
                text_size: self.width, None
                theme_text_color: "Custom"
                text_color: 0.9, 1, 0.9, 1

            MDLabel:
                id: coords_label
                text: "--"
                halign: "center"
                bold: True
                font_size: "16sp"
                shorten: True
                size_hint_y: None
                height: "22dp"
                text_size: self.width, None
                theme_text_color: "Custom"
                text_color: 0.7, 0.9, 0.7, 1

            MDLabel:
                id: main_temp
                text: "--°"
                halign: "center"
                font_size: "56sp"
                bold: True
                size_hint_y: None
                height: "70dp"
                theme_text_color: "Custom"
                text_color: 0.95, 1, 0.95, 1

            # Блок данных под температурой (вертикальный контейнер)
            MDBoxLayout:
                orientation: 'vertical'
                adaptive_height: True
                spacing: "10dp"
                size_hint_x: None
                width: self.minimum_width
                pos_hint: {"center_x": .5}

                # Слой 0 — текущее состояние (иконка + описание)
                MDBoxLayout:
                    orientation: 'horizontal'
                    adaptive_height: True
                    spacing: "6dp"
                    size_hint_x: None
                    width: self.minimum_width
                    pos_hint: {"center_x": .5}
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
                        font_size: "15sp"
                        size_hint: None, None
                        width: self.texture_size[0]
                        height: self.texture_size[1]
                        text_size: None, None
                        halign: 'center'
                        theme_text_color: "Custom"
                        text_color: 0.85, 1, 0.85, 1

                # Слой 1 — восход и закат (горизонтально, далеко друг от друга)
                MDBoxLayout:
                    orientation: 'horizontal'
                    adaptive_height: True
                    spacing: "40dp"
                    size_hint_x: None
                    width: self.minimum_width
                    pos_hint: {"center_x": .5}
                    MDBoxLayout:
                        orientation: 'horizontal'
                        adaptive_height: True
                        spacing: "5dp"
                        size_hint_x: None
                        width: self.minimum_width
                        MDIcon:
                            icon: "weather-sunset-up"
                            font_size: "24sp"
                            size_hint: None, None
                            size: "24dp", "24dp"
                            theme_text_color: "Custom"
                            text_color: 0.95, 0.85, 0.45, 1
                        MDLabel:
                            id: sunrise_label
                            text: "--:--"
                            size_hint: None, None
                            width: self.texture_size[0]
                            height: self.texture_size[1]
                            text_size: None, None
                            halign: 'center'
                            theme_text_color: "Custom"
                            text_color: 0.85, 1, 0.85, 1
                    MDBoxLayout:
                        orientation: 'horizontal'
                        adaptive_height: True
                        spacing: "5dp"
                        size_hint_x: None
                        width: self.minimum_width
                        MDIcon:
                            icon: "weather-sunset-down"
                            font_size: "24sp"
                            size_hint: None, None
                            size: "24dp", "24dp"
                            theme_text_color: "Custom"
                            text_color: 0.95, 0.6, 0.4, 1
                        MDLabel:
                            id: sunset_label
                            text: "--:--"
                            size_hint: None, None
                            width: self.texture_size[0]
                            height: self.texture_size[1]
                            text_size: None, None
                            halign: 'center'
                            theme_text_color: "Custom"
                            text_color: 0.85, 1, 0.85, 1

                # Слой 2 — Обновлено (под восходом/закатом)
                MDBoxLayout:
                    orientation: 'horizontal'
                    adaptive_height: True
                    spacing: "6dp"
                    size_hint_x: None
                    width: self.minimum_width
                    pos_hint: {"center_x": .5}
                    MDIcon:
                        icon: "update"
                        font_size: "16sp"
                        size_hint: None, None
                        size: "16dp", "16dp"
                        theme_text_color: "Custom"
                        text_color: 0.55, 0.75, 0.55, 1
                    MDLabel:
                        id: status_label
                        text: "Система готова"
                        font_size: "13sp"
                        size_hint: None, None
                        width: self.texture_size[0]
                        height: self.texture_size[1]
                        text_size: None, None
                        halign: 'center'
                        theme_text_color: "Custom"
                        text_color: 0.55, 0.75, 0.55, 1

            MDFillRoundFlatButton:
                text: "ОБНОВИТЬ GPS"
                pos_hint: {"center_x": .5}
                size_hint_x: 0.9
                md_bg_color: 0.2, 0.4, 0.2, 1
                on_release: app.run_gps_logic()

        MDLabel:
            text: "ПРОГНОЗ НА 5 ДНЕЙ"
            bold: True
            size_hint_y: None
            height: "24dp"
            theme_text_color: "Custom"
            text_color: 0.6, 0.9, 0.6, 1

        MDScrollView:
            MDList:
                id: forecast_list
                spacing: "6dp"
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
        self._loc_name = ""
        self._gps_watchdog = None
        return Builder.load_string(KV)

    def on_start(self):
        if platform == "android":
            self._request_permissions()
        Clock.schedule_once(self._safe_start, 2)

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

    def _safe_start(self, *args):
        self.load_cache()

    def log(self, *args):
        try:
            print("[WildVantage]", *args)
        except Exception:
            pass

    def _set_status(self, text):
        try:
            self.root.ids.status_label.text = text
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

    # --- АСИНХРОННЫЙ HTTP (Kivy на Android) ---
    def _fetch(self, url, on_success=None, on_error=None, timeout=None, headers=None):
        kwargs = {}
        if certifi is not None:
            kwargs["ca_file"] = certifi.where()
        req = [None]
        state = {"done": False}

        def wrap(cb):
            def wrapped(*a):
                if state["done"]:
                    return
                state["done"] = True
                cb(*a)

            return wrapped

        def abort(_dt):
            if not state["done"]:
                state["done"] = True
                if req[0] is not None:
                    try:
                        req[0].cancel()
                    except Exception:
                        pass
                if on_error:
                    on_error(None, Exception("timeout"))

        if timeout is not None:
            Clock.schedule_once(abort, timeout)
        try:
            req[0] = UrlRequest(
                url,
                on_success=wrap(on_success) if on_success else None,
                on_failure=wrap(on_error) if on_error else None,
                on_error=wrap(on_error) if on_error else None,
                verify=True,
                req_headers=headers,
                **kwargs,
            )
        except Exception:
            if on_error:
                on_error(None, Exception("http init"))

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

    def on_geocode_error(self, *args):
        self._set_status("Город не найден (нет сети)")

    # --- GPS ---
    def run_gps_logic(self):
        self._set_status("Поиск спутников...")
        if gps is None:
            self._set_status("GPS недоступен")
            return
        try:
            gps.configure(on_location=self.on_gps_loc, on_status=self.on_gps_status)
        except Exception:
            try:
                gps.configure(on_location=self.on_gps_loc)
            except Exception:
                self._set_status("Ошибка GPS: включите спутники")
                return
        try:
            gps.start(minTime=1000, minDistance=0)
        except Exception:
            self._set_status("Ошибка GPS: включите спутники")
            return
        self._arm_gps_watchdog()

    def _arm_gps_watchdog(self):
        self._cancel_gps_watchdog()
        self._gps_watchdog = Clock.schedule_once(self._gps_timeout, GPS_TIMEOUT)

    def _cancel_gps_watchdog(self):
        if self._gps_watchdog is not None:
            try:
                self._gps_watchdog.cancel()
            except Exception:
                pass
            self._gps_watchdog = None

    def _gps_timeout(self, *args):
        self._gps_watchdog = None
        self._stop_gps()
        self._set_status("GPS: нет сигнала. Проверьте небо и разрешение.")

    def _stop_gps(self):
        try:
            gps.stop()
        except Exception:
            pass

    def on_gps_status(self, **kwargs):
        self.log("gps status:", kwargs)

    def on_gps_loc(self, **kwargs):
        lat = kwargs.get("lat")
        lon = kwargs.get("lon")
        if lat is None or lon is None:
            self._set_status("GPS: координаты не получены")
            return
        self._cancel_gps_watchdog()
        self._stop_gps()
        try:
            lat = float(lat)
            lon = float(lon)
        except Exception:
            self._set_status("GPS: некорректные координаты")
            return
        accuracy = kwargs.get("accuracy")
        self.log("gps fix:", lat, lon, "accuracy=", accuracy)
        timeout = 3 if self.is_wilderness else 20
        self.start_update(lat, lon, accuracy=accuracy, source="gps", timeout=timeout)

    # --- ЗАПУСК ОБНОВЛЕНИЯ (единая точка, защита от гонок) ---
    def start_update(self, lat, lon, accuracy=None, source="gps", display_name=None, timeout=None):
        self._gen += 1
        gen = self._gen
        self._last_weather = None
        self._last_coords = {"lat": lat, "lon": lon, "accuracy": accuracy}
        self._set_coords_text(lat, lon, accuracy)
        try:
            root = self.root.ids
            root.forecast_list.clear_widgets()
            root.main_temp.text = "--°"
            root.sunrise_label.text = "--:--"
            root.sunset_label.text = "--:--"
            root.current_desc_label.text = "Загрузка..."
            root.current_icon.icon = "update"
        except Exception:
            pass

        if display_name:
            self._set_location_name(display_name)
        else:
            self._set_location_name(None)
            try:
                self.root.ids.loc_label.text = "Определяю место..."
            except Exception:
                pass
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
            self.log(
                "nominatim address:",
                {k: address.get(k) for k in ("suburb", "city_district", "neighbourhood", "quarter", "city", "town", "village", "municipality", "county")}
                if isinstance(address, dict)
                else None,
            )
            name = pick_location_name(address)
            self.log("location name ->", name)
            if name:
                self._set_location_name(name)
                self._save_cache()
            else:
                self._set_location_name(f"{lat:.5f}, {lon:.5f}")

        def error(_req, _err):
            self.log("nominatim error:", _err)
            if gen == self._gen:
                self._set_location_name(f"{lat:.5f}, {lon:.5f}")

        self._fetch(url, on_success=success, on_error=error, timeout=12, headers=headers)

    # --- ПОГОДА (Open-Meteo) ---
    def fetch_weather(self, lat, lon, gen, timeout=None):
        url = OPEN_METEO_URL.format(lat=lat, lon=lon)
        self.log("weather request coords:", lat, lon)

        def success(_req, result):
            if gen != self._gen:
                return
            weather = normalize_weather(result)
            if not weather or not weather.get("daily"):
                self.log("weather: пустой ответ")
                self.load_cache(gen=gen, show_err=True, requested=self._last_coords)
                return
            self._last_weather = weather
            self.log(
                "weather:",
                "tz=", weather.get("timezone"),
                "utc_offset=", weather.get("utc_offset_seconds"),
                "time=", weather["current"].get("time"),
                "temp=", weather["current"].get("temp"),
                "code=", weather["current"].get("code"),
                "cloud=", weather["current"].get("cloud_cover"),
                "is_day=", weather["current"].get("is_day"),
            )
            self.process_weather(weather, self._last_coords)
            self._save_cache()

        def error(_req, err):
            if gen != self._gen:
                return
            self.log("weather error:", err)
            self.load_cache(gen=gen, show_err=True, requested=self._last_coords)

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
            if coords.get("lat") is not None and coords.get("lon") is not None:
                self._set_coords_text(coords["lat"], coords["lon"], coords.get("accuracy"))
            if self._loc_name:
                root.loc_label.text = self._loc_name
            root.forecast_list.clear_widgets()
        except Exception:
            pass

        if days:
            today = days[0]
            self.log("sunrise=", fmt_hhmm(today.get("sunrise")), "sunset=", fmt_hhmm(today.get("sunset")))
            try:
                self.root.ids.sunrise_label.text = fmt_hhmm(today.get("sunrise"))
                self.root.ids.sunset_label.text = fmt_hhmm(today.get("sunset"))
            except Exception:
                pass
            self._fill_forecast(days[:5])

        if from_cache:
            self._set_status("⚠️ Нет сети. Данные из кэша.")
        else:
            self._set_status(f"Обновлено: {fmt_hhmm(cur.get('time'))} ({weather.get('timezone') or '—'})")

    def _fill_forecast(self, days):
        try:
            forecast_list = self.root.ids.forecast_list
        except Exception:
            return
        forecast_list.clear_widgets()
        for day in days:
            code = day.get("code")
            d_desc, d_icon = wmo_info(code, True)
            text = f"{fmt_ddmm(day.get('date'))}  |  {fmt_temp(day.get('tmax'))} / {fmt_temp(day.get('tmin'))}"
            item = ThreeLineIconListItem(text=text, secondary_text=d_desc)
            item.add_widget(IconLeftWidget(icon=d_icon))
            forecast_list.add_widget(item)

    # --- КЭШ ---
    def _save_cache(self):
        if not self._last_weather or not self._last_coords:
            return
        record = {
            "coords": self._last_coords,
            "location_name": self._loc_name,
            "weather": self._last_weather,
            "saved_at": datetime.now().strftime("%d.%m %H:%M"),
        }
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False)
        except Exception:
            pass

    def load_cache(self, gen=None, city_filter=None, show_err=False, requested=None):
        if gen is not None and gen != self._gen:
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                record = json.load(f)
        except Exception:
            self._set_status("Кэш пуст — включите интернет")
            return

        if city_filter:
            name = record.get("location_name") or ""
            if city_filter.lower() not in name.lower():
                self._set_status("Город не найден в кэше")
                return

        weather = record.get("weather")
        coords = record.get("coords") if isinstance(record.get("coords"), dict) else {}
        if not isinstance(weather, dict) or not weather.get("daily"):
            self._set_status("Кэш повреждён — включите интернет")
            return

        self._last_weather = weather
        self._last_coords = coords
        self._set_location_name(record.get("location_name") or "Кэш")
        self.process_weather(weather, coords, from_cache=True)

        if show_err:
            if requested and self._coords_far(requested, coords):
                self._set_status("⚠️ Нет сети. Кэш от другого места.")
            else:
                self._set_status("⚠️ Нет сети. Данные из кэша.")
        elif city_filter:
            self._set_status("Офлайн: данные из кэша")

    def _coords_far(self, a, b):
        try:
            return abs(float(a["lat"]) - float(b["lat"])) > 0.05 or abs(
                float(a["lon"]) - float(b["lon"])
            ) > 0.05
        except Exception:
            return False


if __name__ == "__main__":
    WildVantage().run()
