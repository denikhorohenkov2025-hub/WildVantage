import json
import os
import time
import urllib.parse
from datetime import datetime

from kivy.clock import Clock
from kivy.utils import platform
from kivy.lang import Builder
from kivy.network.urlrequest import UrlRequest
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

CACHE_FILE = "wildvantage_v4.json"
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


def updated_status_text(iso):
    """Компактная строка статуса. Timezone в UI НЕ показываем."""
    return f"Обновлено: {fmt_hhmm(iso)}"


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
    """Часовые значения, начиная с часа 'сейчас' (для «Ближайшие 24 ч»)."""
    if not hours:
        return []
    start = 0
    if isinstance(current_iso, str) and len(current_iso) >= 13 and "T" in current_iso:
        cur = current_iso[11:13]
        for i, h in enumerate(hours):
            t = h.get("time")
            if isinstance(t, str) and len(t) >= 13 and t[11:13] == cur:
                start = i
                break
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
                height: "40dp"
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

            # Переключатель режима
            MDBoxLayout:
                orientation: 'horizontal'
                size_hint_y: None
                height: self.minimum_height
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
                    height: "30dp"
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
                    height: "22dp"
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
                    height: "66dp"
                    valign: "middle"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: 0.95, 1, 0.95, 1

                # Детали «сейчас»: ощущается | влажность | ветер
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: "24dp"
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
                            MDLabel:
                                id: feels_label
                                text: "--°"
                                font_size: "15sp"
                                size_hint: None, None
                                width: "64dp"
                                height: "22dp"
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
                            MDLabel:
                                id: humidity_label
                                text: "--%"
                                font_size: "15sp"
                                size_hint: None, None
                                width: "64dp"
                                height: "22dp"
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
                            MDLabel:
                                id: wind_label
                                text: "-- м/с"
                                font_size: "15sp"
                                size_hint: None, None
                                width: "80dp"
                                height: "22dp"
                                theme_text_color: "Custom"
                                text_color: 0.85, 1, 0.85, 1

                # Состояние: иконка + описание
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: self.minimum_height
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
                            width: "235dp"
                            height: "24dp"
                            shorten: True
                            text_size: self.width, None
                            halign: "center"
                            valign: "middle"
                            theme_text_color: "Custom"
                            text_color: 0.85, 1, 0.85, 1
                    MDBoxLayout:
                        size_hint_x: 1

                # Восход / закат
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: "24dp"
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
                                size: "22dp", "22dp"
                                theme_text_color: "Custom"
                                text_color: 0.95, 0.85, 0.45, 1
                            MDLabel:
                                id: sunrise_label
                                text: "--:--"
                                font_size: "16sp"
                                size_hint: None, None
                                width: "52dp"
                                height: "24dp"
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
                                size: "22dp", "22dp"
                                theme_text_color: "Custom"
                                text_color: 0.95, 0.6, 0.4, 1
                            MDLabel:
                                id: sunset_label
                                text: "--:--"
                                font_size: "16sp"
                                size_hint: None, None
                                width: "52dp"
                                height: "24dp"
                                theme_text_color: "Custom"
                                text_color: 0.85, 1, 0.85, 1

                # Статус обновления
                MDLabel:
                    id: status_label
                    text: "Система готова"
                    halign: "center"
                    valign: "middle"
                    font_size: "13sp"
                    size_hint_y: None
                    height: "20dp"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: 0.55, 0.75, 0.55, 1

                # Кнопка Обновить GPS (центрируется по ширине)
                MDBoxLayout:
                    orientation: 'horizontal'
                    size_hint_y: None
                    height: self.minimum_height
                    MDBoxLayout:
                        size_hint_x: 1
                    MDFillRoundFlatButton:
                        text: "ОБНОВИТЬ GPS"
                        size_hint_x: None
                        width: "240dp"
                        size_hint_y: None
                        height: "44dp"
                        md_bg_color: 0.2, 0.4, 0.2, 1
                        on_release: app.run_gps_logic()
                    MDBoxLayout:
                        size_hint_x: 1

            # Ближайшие 24 ч — горизонтальный скролл
            MDLabel:
                text: "БЛИЖАЙШИЕ 24 Ч"
                bold: True
                size_hint_y: None
                height: "20dp"
                theme_text_color: "Custom"
                text_color: 0.6, 0.9, 0.6, 1

            MDScrollView:
                size_hint_y: None
                height: "92dp"
                do_scroll_x: True
                do_scroll_y: False
                bar_width: "3dp"
                MDBoxLayout:
                    id: hourly_row
                    orientation: 'horizontal'
                    size_hint_x: None
                    width: self.minimum_width
                    size_hint_y: None
                    height: "88dp"
                    spacing: "6dp"

            MDLabel:
                text: "ПРОГНОЗ НА 7 ДНЕЙ"
                bold: True
                size_hint_y: None
                height: "20dp"
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
        self._loc_name = ""
        self._reset_gps_state()
        return Builder.load_string(KV)

    def on_start(self):
        if platform == "android":
            self._request_permissions()
        self._set_safe_areas()
        Clock.schedule_once(self._safe_start, 2)

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
        try:
            self.load_cache()
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
                try:
                    cb(*a)
                except Exception as e:
                    # Ошибка в сетевом колбэке не должна ронять процесс:
                    # логируем полный traceback и продолжаем работать.
                    self._log_exc("network callback", e)
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
        self._set_status("Поиск спутников…")
        if gps is None:
            self._set_status("GPS недоступен")
            return
        if not self._has_fine_location():
            self._request_permissions()
            self._set_status("Нужно разрешение на точное местоположение")
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
                self._set_status("Ошибка GPS: включите спутники")
                return
        try:
            # частые обновления: было 1000 мс, теперь 500 мс
            gps.start(minTime=500, minDistance=0)
        except Exception:
            self._set_status("Ошибка GPS: включите спутники")
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
                self._set_status("GPS выключен — включите спутники")
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
        self._set_status(f"Уточнение GPS… ±{best_acc:.0f} м")

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
            self._set_status("GPS: нет сигнала. Проверьте небо и разрешение.")
            return
        self.log(
            "gps: завершение —", reason,
            "| измерений:", len(self._gps_fixes),
            "| лучший ±", best["accuracy"], "м",
        )
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
                self._save_cache()
            else:
                self._set_location_name(f"{lat:.5f}, {lon:.5f}")

        def error(_req, _err):
            self.log("geocode: nominatim error:", _err)
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

        self._fill_hours(slice_hours(weather.get("hours") or [], cur.get("time")))

        if days:
            today = days[0]
            self.log("sunrise=", fmt_hhmm(today.get("sunrise")), "sunset=", fmt_hhmm(today.get("sunset")))
            try:
                self.root.ids.sunrise_label.text = fmt_hhmm(today.get("sunrise"))
                self.root.ids.sunset_label.text = fmt_hhmm(today.get("sunset"))
            except Exception:
                pass
            self._fill_forecast(days[:7])

        if from_cache:
            self._set_status("⚠️ Нет сети. Данные из кэша.")
        else:
            self._set_status(updated_status_text(cur.get("time")))

    def _fill_hours(self, hours):
        """Строит почасовые ячейки «Ближайшие 24 ч» (иконка, время, °)."""
        try:
            row = self.root.ids.hourly_row
        except Exception:
            return
        row.clear_widgets()
        for h in hours:
            try:
                self._build_hour_cell(row, h)
            except Exception as e:
                self._log_exc("hour cell", e)

    def _build_hour_cell(self, row, h):
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
            size=("52dp", "88dp"),
        )
        box.add_widget(
            MDLabel(
                text=fmt_hhmm(h.get("time")),
                font_size="12sp",
                halign="center",
                size_hint=(None, None),
                size=("52dp", "18dp"),
                text_size=(None, None),
                theme_text_color="Custom",
                text_color=(0.7, 0.85, 0.7, 1),
            )
        )
        box.add_widget(
            MDIcon(
                icon=icon,
                font_size="22sp",
                halign="center",
                theme_text_color="Custom",
                text_color=(0.85, 1, 0.85, 1),
                size_hint=(None, None),
                size=("52dp", "28dp"),
            )
        )
        box.add_widget(
            MDLabel(
                text=fmt_temp(h.get("temp")),
                font_size="14sp",
                bold=True,
                halign="center",
                size_hint=(None, None),
                size=("52dp", "20dp"),
                text_size=(None, None),
                theme_text_color="Custom",
                text_color=(0.95, 1, 0.95, 1),
            )
        )
        row.add_widget(box)

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
        else:
            self._set_location_name(record.get("location_name") or "Кэш")
        self.process_weather(weather, coords, from_cache=True)

        if show_err:
            if is_far:
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
