import json
import math
from datetime import datetime

from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp, sp
from kivy.network.urlrequest import UrlRequest
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.widget import Widget
from kivy.utils import platform
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDIconButton, MDFillRoundFlatButton
from kivymd.uix.card import MDCard
from kivymd.uix.label import MDLabel, MDIcon
from kivymd.uix.list import MDList
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.selectioncontrol import MDSwitch
from kivymd.uix.textfield import MDTextField

try:
    import certifi
except Exception:
    certifi = None

gps = compass = None

API_KEY = "5dfb720a2f0c5b0c7d131f88236baecf"
CACHE_FILE = "wildvantage_v2.json"
DIRECTIONS = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]

KV = """
MDBoxLayout:
    orientation: "vertical"
    md_bg_color: 0.05, 0.08, 0.05, 1
    padding: 0
    spacing: 0

    MDCard:
        id: header
        orientation: "horizontal"
        size_hint_y: None
        height: dp(58)
        padding: dp(16), dp(8)
        spacing: dp(8)
        radius: [dp(18),]
        elevation: 0
        md_bg_color: 0.08, 0.12, 0.08, 1
        line_color: 0.2, 0.4, 0.2, 1
        MDLabel:
            text: "WILDVANTAGE"
            bold: True
            font_size: sp(17)
            halign: "left"
            theme_text_color: "Custom"
            text_color: 0.9, 1, 0.9, 1
        MDIconButton:
            icon: "crosshairs-gps"
            on_release: app.start_gps()
            theme_icon_color: "Custom"
            icon_color: 0.6, 0.9, 0.6, 1

    MDScrollView:
        MDBoxLayout:
            orientation: "vertical"
            padding: dp(12)
            spacing: dp(12)
            size_hint_y: None
            height: self.minimum_height

            # Слой 1 — Поиск города
            MDBoxLayout:
                orientation: "vertical"
                adaptive_height: True
                spacing: dp(15)
                padding: dp(10)

                MDBoxLayout:
                    orientation: "horizontal"
                    adaptive_height: True
                    spacing: dp(8)
                    padding: dp(12), dp(4)
                    md_bg_color: 0.12, 0.18, 0.12, 1
                    radius: [dp(14),]
                    line_color: 0.2, 0.4, 0.2, 1
                    MDTextField:
                        id: search_field
                        hint_text: "Введите город"
                        size_hint_x: 1
                        size_hint_y: None
                        height: dp(48)
                        mode: "round"
                        input_type: "text"
                        input_filter: None
                        keyboard_suggestions: True
                        helper_text_mode: "on_error"
                    MDIconButton:
                        icon: "magnify"
                        on_release: app.search_city()
                        theme_icon_color: "Custom"
                        icon_color: 0.6, 0.9, 0.6, 1

                # Слой 2 — Режим Глуши (строго под поиском)
                MDBoxLayout:
                    orientation: "horizontal"
                    adaptive_height: True
                    spacing: dp(8)
                    padding: dp(12), dp(4)
                    md_bg_color: 0.12, 0.18, 0.12, 1
                    radius: [dp(14),]
                    line_color: 0.2, 0.4, 0.2, 1
                    MDLabel:
                        id: mode_text
                        text: "РЕЖИМ ГОРОД (ONLINE)"
                        bold: True
                        theme_text_color: "Custom"
                        text_color: 0.85, 1, 0.85, 1
                    Widget:
                    MDSwitch:
                        id: mode_switch
                        active: False
                        on_active: app.toggle_mode(*args)

            # Главная карточка: город, координаты, азимут, температура, солнце
            MDCard:
                id: info_card
                orientation: "vertical"
                size_hint_y: None
                height: dp(355)
                padding: dp(16)
                spacing: dp(6)
                radius: [dp(18),]
                elevation: 0
                md_bg_color: 0.12, 0.18, 0.12, 1
                line_color: 0.2, 0.4, 0.2, 1
                MDLabel:
                    id: loc_label
                    text: "Ожидание данных..."
                    halign: "center"
                    font_style: "H6"
                    font_size: sp(25)
                    bold: True
                    theme_text_color: "Custom"
                    text_color: 0.9, 1, 0.9, 1
                MDLabel:
                    id: azimuth_label
                    text: "Азимут: --"
                    halign: "center"
                    font_size: sp(17)
                    theme_text_color: "Custom"
                    text_color: 0.6, 0.8, 0.6, 1
                MDLabel:
                    id: temp_label
                    text: "--°C"
                    halign: "center"
                    font_size: sp(56)
                    bold: True
                    theme_text_color: "Custom"
                    text_color: 0.95, 1, 0.95, 1
                MDBoxLayout:
                    orientation: "horizontal"
                    size_hint_y: None
                    height: dp(36)
                    Widget:
                    MDIcon:
                        icon: "weather-sunset-up"
                        font_size: "24sp"
                        theme_text_color: "Custom"
                        text_color: 0.75, 1, 0.75, 1
                    MDLabel:
                        id: sr_label
                        text: "--:--"
                        size_hint_x: None
                        width: dp(48)
                        halign: "center"
                        theme_text_color: "Custom"
                        text_color: 0.85, 1, 0.85, 1
                    Widget:
                        size_hint_x: None
                        width: dp(14)
                    MDIcon:
                        icon: "weather-sunset-down"
                        font_size: "24sp"
                        theme_text_color: "Custom"
                        text_color: 0.75, 1, 0.75, 1
                    MDLabel:
                        id: ss_label
                        text: "--:--"
                        size_hint_x: None
                        width: dp(48)
                        halign: "center"
                        theme_text_color: "Custom"
                        text_color: 0.85, 1, 0.85, 1
                    Widget:
                MDLabel:
                    id: status_label
                    text: "Загрузка..."
                    halign: "center"
                    font_size: sp(13)
                    theme_text_color: "Custom"
                    text_color: 0.55, 0.75, 0.55, 1
                MDBoxLayout:
                    orientation: "horizontal"
                    size_hint_y: None
                    height: dp(52)
                    Widget:
                    MDFillRoundFlatButton:
                        text: "ОБНОВИТЬ GPS"
                        size_hint_x: 0.9
                        size_hint_y: None
                        height: dp(48)
                        md_bg_color: 0.2, 0.4, 0.2, 1
                        on_release: app.start_gps()
                    Widget:

            MDLabel:
                text: "ПРОГНОЗ НА 5 ДНЕЙ"
                font_size: sp(14)
                bold: True
                theme_text_color: "Custom"
                text_color: 0.6, 0.9, 0.6, 1
            MDList:
                id: data_list
                spacing: dp(8)
                size_hint_y: None
                height: self.minimum_height
"""


class WildVantage(MDApp):
    def build(self):
        self.title = "WildVantage"
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Green"
        self.theme_cls.primary_hue = "900"

        self.api_key = API_KEY
        self.is_wilderness = False
        self.current_lat = None
        self.current_lon = None
        self._last_az = 0
        self._last_bearing = None
        self._last_bearing_ts = 0

        return Builder.load_string(KV)

    # --- Safe Boot: каркас UI уже показан, датчики/разрешения — через 2 сек ---
    def delayed_init(self, *args):
        self._set_status("Проверка датчиков и разрешений...")
        self._import_sensors()
        self._request_permissions()
        self.start_sensors()
        Clock.schedule_once(self._restart_compass, 0.6)
        self.load_from_cache()

    def _set_status(self, text):
        try:
            self.root.ids.status_label.text = text
        except Exception:
            pass

    def _import_sensors(self):
        global gps, compass
        try:
            from plyer import compass
        except Exception:
            compass = None
        try:
            from plyer import gps
        except Exception:
            gps = None

    def _request_permissions(self):
        if platform != "android":
            return
        try:
            from android.permissions import request_permissions, Permission

            request_permissions(
                [
                    Permission.ACCESS_FINE_LOCATION,
                    Permission.ACCESS_COARSE_LOCATION,
                    Permission.INTERNET,
                    Permission.ACCESS_NETWORK_STATE,
                ]
            )
        except Exception:
            pass

    # --- КОМПАС ---
    def start_sensors(self):
        if compass is None:
            self._no_compass()
        else:
            try:
                compass.enable()
                Clock.schedule_interval(self.update_compass, 1 / 10)
            except Exception:
                self._no_compass()

    # Принудительный перезапуск датчика: disable() -> enable()
    def _restart_compass(self, *args):
        if compass is None:
            return
        try:
            compass.disable()
        except Exception:
            pass

        def enable_later(*_):
            try:
                compass.enable()
            except Exception:
                pass

        Clock.schedule_once(enable_later, 0.3)

    def _no_compass(self):
        try:
            self.root.ids.azimuth_label.text = "Азимут: -- (Датчик не найден)"
        except Exception:
            pass

    def update_compass(self, dt):
        if compass is None:
            return
        from time import time

        try:
            val = compass.field
            self._last_az = time()
            if not val:
                self.root.ids.azimuth_label.text = "Азимут: -- (Датчик не найден)"
                return
            bearing = (math.degrees(math.atan2(val[1], val[0])) + 360) % 360
            idx = int((bearing + 22.5) // 45) % 8
            msg = f"Азимут: {int(bearing)}° ({DIRECTIONS[idx]})"
            now = time()
            if (
                self._last_bearing is not None
                and abs(bearing - self._last_bearing) < 1
                and now - self._last_bearing_ts > 4
            ):
                msg += " · Откалибруйте (8)"
            elif abs(bearing - (self._last_bearing or -999)) >= 1:
                self._last_bearing = bearing
                self._last_bearing_ts = now
            self.root.ids.azimuth_label.text = msg
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
                root.header.md_bg_color = 0.15, 0.05, 0.05, 1
            else:
                root.mode_text.text = "РЕЖИМ ГОРОД (ONLINE)"
                root.mode_text.text_color = 0.85, 1, 0.85, 1
                root.header.md_bg_color = 0.08, 0.12, 0.08, 1
        except Exception:
            pass

    # --- GPS ---
    def start_gps(self, *args):
        self._restart_compass()
        self._set_status("📡 Поиск спутников (в лесу до 2-х минут)...")
        if gps is None:
            self._set_status("GPS недоступен")
            return
        try:
            gps.configure(on_location=self.on_location)
            gps.start()
        except Exception:
            self._set_status("Ошибка GPS: включите спутники")

    def on_location(self, **kwargs):
        lat = kwargs.get("lat")
        lon = kwargs.get("lon")
        self.current_lat = lat
        self.current_lon = lon
        try:
            gps.stop()
        except Exception:
            pass
        if not lat or not lon:
            self._set_status("GPS: координаты не получены")
            return
        if self.is_wilderness:
            self._gps_offline(lat, lon)
            return
        self._show_offline_point(lat, lon)
        self.fetch_weather_by_coords(lat, lon, gps_mode=True)

    # Точка GPS: сразу координаты + солнце (офлайн, без ожидания API)
    def _show_offline_point(self, lat, lon):
        try:
            tz_offset = self.estimate_timezone(lon)
            today = datetime.now().date()
            sr, ss = self.local_sun_times(lat, lon, tz_offset, today)
            self.root.ids.loc_label.text = f"Точка GPS\n{lat:.4f}, {lon:.4f}"
            self.root.ids.temp_label.text = "--°C"
            self.root.ids.sr_label.text = sr
            self.root.ids.ss_label.text = ss
            self.root.ids.data_list.clear_widgets()
        except Exception:
            pass

# Режим «Глушь»: гибрид — спутники + быстрый сетевой запрос.
    # Есть интернет -> обновим онлайн; нет -> сообщение + кэш + пересчёт солнца.
    def _gps_offline(self, lat, lon):
        self._show_offline_point(lat, lon)
        self.fetch_weather_by_coords(lat, lon, gps_mode=True, timeout=3)

    # --- АСИНХРОННЫЙ HTTP (UrlRequest, не блокирует интерфейс) ---
    def _request_json(self, url, on_success, on_http, on_error, timeout=None):
        self._set_status("⏳ Загрузка прогноза...")
        kwargs = {}
        if certifi is not None:
            kwargs["ca_file"] = certifi.where()
        req = None
        done = {"ok": False}

        if timeout is not None:
            def wrap(cb):
                def wrapped(*args):
                    done["ok"] = True
                    cb(*args)
                return wrapped

            def timeout_abort(_dt):
                if not done["ok"]:
                    done["ok"] = True
                    if req is not None:
                        try:
                            req.cancel()
                        except Exception:
                            pass
                    on_error(None, Exception("timeout"))

            Clock.schedule_once(timeout_abort, timeout)
        try:
            req = UrlRequest(
                url,
                on_success=(wrap(on_success) if timeout is not None else on_success),
                on_failure=(wrap(on_http) if timeout is not None else on_http),
                on_error=(wrap(on_error) if timeout is not None else on_error),
                verify=True,
                **kwargs,
            )
        except Exception:
            on_error(None, Exception("http init"))

    # --- ПОИСК ГОРОДА (онлайн, с кириллицей) ---
    def search_city(self, *args):
        city = self.root.ids.search_field.text.strip()
        if not city:
            self._set_status("Введите город")
            return
        if self.is_wilderness:
            self._search_offline_cache(city)
            return
        from urllib.parse import quote

        url = (
            f"https://api.openweathermap.org/geo/1.0/direct"
            f"?q={quote(city)}&limit=1&appid={self.api_key}"
        )

        def success(request, result):
            try:
                if isinstance(result, list) and result:
                    loc = result[0]
                    self.current_lat = loc["lat"]
                    self.current_lon = loc["lon"]
                    self.fetch_weather_by_coords(loc["lat"], loc["lon"])
                else:
                    self._set_status("Город не найден")
            except Exception:
                self._set_status("Город не найден")

        def http_error(request, _body):
            status = getattr(request, "resp_status", None)
            if status == 401:
                self._set_status("Ошибка ключа (401) — данные из кэша")
            else:
                self._set_status("API ошибка — город не найден")
            self.load_from_cache()

        def net_error(_request, _error):
            self._set_status("Нет сети — данные из кэша")
            self.load_from_cache()

        self._request_json(url, success, http_error, net_error)

    # --- ПОГОДА (онлайн) ---
    def fetch_weather_by_coords(self, lat, lon, gps_mode=False, timeout=None):
        url = (
            f"https://api.openweathermap.org/data/2.5/forecast"
            f"?lat={lat}&lon={lon}&appid={self.api_key}&units=metric&lang=ru"
        )

        def success(request, result):
            try:
                if not isinstance(result, dict) or "city" not in result:
                    raise ValueError("bad json")
                data = result
                data["saved_at"] = datetime.now().strftime("%d.%m %H:%M")
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f)
                self.refresh_ui(data)
            except Exception:
                if gps_mode and self.is_wilderness:
                    self._fallback_cache(lat, lon)
                elif gps_mode:
                    self._set_status("Спутники: ОК | Погода: Ошибка данных")
                else:
                    self._set_status("Ошибка данных — из кэша")
                    self.load_from_cache()

        def http_error(request, _body):
            status = getattr(request, "resp_status", None)
            if gps_mode and self.is_wilderness:
                self._fallback_cache(lat, lon)
            elif status == 401:
                if gps_mode:
                    self._set_status("Спутники: ОК | Погода: Ожидание API (401)")
                else:
                    self._set_status("Ошибка ключа (401) — данные из кэша")
                    self.load_from_cache()
            else:
                if gps_mode:
                    self._set_status(
                        f"Спутники: ОК | Погода: API ошибка ({status})"
                    )
                else:
                    self._set_status("API ошибка — данные из кэша")
                    self.load_from_cache()

        def net_error(_request, _error):
            if gps_mode and self.is_wilderness:
                self._fallback_cache(lat, lon)
            elif gps_mode:
                self._set_status("Спутники: ОК | Погода: Нет сети")
            else:
                self._set_status("Нет сети — данные из кэша")
                self.load_from_cache()

        self._request_json(url, success, http_error, net_error, timeout=timeout)

    # Глушь + нет сети: статус, загрузка из кэша, пересчёт солнца для координат
    def _fallback_cache(self, lat, lon):
        self._set_status("⚠️ Нет сети. Прогноз из кэша.")
        self.load_from_cache(new_lat=lat, new_lon=lon)

    # --- УМНЫЙ КЭШ ---
    def load_from_cache(self, new_lat=None, new_lon=None):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            if new_lat and new_lon:
                data["city"]["coord"] = {"lat": new_lat, "lon": new_lon}
                data["city"]["name"] = "Точка GPS"
                if "timezone" not in data["city"]:
                    data["city"]["timezone"] = self.estimate_timezone(new_lon)
            self.refresh_ui(data, cache=True)
        except Exception:
            self._set_status("Нет данных (кэш пуст)")

    # Офлайн-поиск в Глуши: ищем город в wildvantage_v2.json
    def _search_offline_cache(self, city):
        target = city.lower().strip()
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            cached_name = data.get("city", {}).get("name", "").lower()
            if cached_name and target and target in cached_name:
                self.current_lat = data["city"]["coord"]["lat"]
                self.current_lon = data["city"]["coord"]["lon"]
                self.refresh_ui(data, cache=True)
                self._set_status(f"Офлайн: {city} — данные из кэша")
            else:
                self._set_status(f"Нет данных о «{city}» в офлайн-истории")
        except Exception:
            self._set_status("Кэш пуст — включите интернет")

    def estimate_timezone(self, lon):
        try:
            return int(round(lon / 15.0) * 3600)
        except Exception:
            return 0

    def local_sun_times(self, lat, lon, tz_offset, date):
        try:
            from astral import LocationInfo
            from astral.sun import sun
            import pytz

            loc = LocationInfo("", "", "UTC", lat, lon)
            s = sun(loc.observer, date=date)
            local_tz = pytz.FixedOffset(int(tz_offset // 60))
            sunrise = s["sunrise"].astimezone(local_tz).strftime("%H:%M")
            sunset = s["sunset"].astimezone(local_tz).strftime("%H:%M")
            return sunrise, sunset
        except Exception:
            return "--:--", "--:--"

    # --- УМНЫЕ ИКОНКИ ПОГОДЫ (keyword mapping по русскому описанию) ---
    def get_weather_icon(self, description):
        d = description.lower()
        if any(w in d for w in ("гроз", "шторм")):
            return "weather-lightning-rainy"
        if any(w in d for w in ("изморось", "морось", "небольш")):
            return "weather-partly-rainy"
        if any(w in d for w in ("ливень", "сильн", "дождь")):
            return "weather-pouring"
        if any(w in d for w in ("снег", "метель", "снегопад")):
            return "weather-snowy-heavy"
        if any(w in d for w in ("туман", "дымка", "мгла", "пыль")):
            return "weather-fog"
        if any(w in d for w in ("ясно", "солн")):
            return "weather-sunny"
        if any(w in d for w in ("пасмур", "облач", "перемен")):
            return "weather-cloudy"
        return "weather-sunny"

    # --- ФИЛЬТР: ОДНА ЗАПИСЬ НА 12:00 КАЖДОГО ДНЯ ---

    # --- ГРУППИРОВКА ПО ДНЯМ: МАКС/МИН температуры за сутки ---
    def _daily_extremes(self, forecast_list):
        try:
            days = {}
            for f in forecast_list:
                if "dt" not in f or "main" not in f:
                    continue
                dt = datetime.fromtimestamp(f["dt"])
                days.setdefault(dt.date(), []).append(f)
            result = []
            for day in sorted(days):
                entries = days[day]
                tmax = max(e["main"].get("temp_max", e["main"].get("temp"))
                           for e in entries)
                tmin = min(e["main"].get("temp_min", e["main"].get("temp"))
                           for e in entries)
                exact = [e for e in entries if "12:00:00" in e.get("dt_txt", "")]
                rep = exact[0] if exact else min(
                    entries, key=lambda e: abs(
                        datetime.fromtimestamp(e["dt"]).hour - 12))
                result.append({"date": day, "tmax": tmax, "tmin": tmin, "rep": rep})
            return result
        except Exception:
            return []

    # --- ОБНОВЛЕНИЕ ИНТЕРФЕЙСА ---
    def refresh_ui(self, data, cache=False):
        try:
            self.root.ids.data_list.clear_widgets()

            lat = data["city"]["coord"]["lat"]
            lon = data["city"]["coord"]["lon"]
            tz_offset = data["city"].get("timezone", self.estimate_timezone(lon))

            self.root.ids.loc_label.text = (
                f"{data['city']['name']}\n{lat:.4f}, {lon:.4f}"
            )
            days = self._daily_extremes(data.get("list", []))[:5]
            if days:
                today = days[0]
                self.root.ids.temp_label.font_size = sp(56)
                self.root.ids.temp_label.text = (
                    f"{int(today['tmax'])}° / {int(today['tmin'])}°"
                )
                self._set_status(
                    f"Обновлено: {data.get('saved_at', 'Неизвестно')}"
                    + ("  (кэш)" if cache else "")
                )
            else:
                self.root.ids.temp_label.text = "--°C"
                self._set_status("Прогноз временно недоступен (обновите позже)")

            today = datetime.now().date()
            sunrise, sunset = self.local_sun_times(lat, lon, tz_offset, today)
            self.root.ids.sr_label.text = sunrise
            self.root.ids.ss_label.text = sunset

            days = self._daily_extremes(data.get("list", []))
            if not days:
                return
            for day in days:
                self._add_forecast_row(day, lat, lon, tz_offset)
        except Exception:
            self._set_status("Ошибка отображения данных")
            self.load_from_cache()

    # --- СТРОКА ПРОГНОЗА: дата, МАКС°/МИН°, иконки солнца ---
    def _add_forecast_row(self, day, lat, lon, tz_offset):
        try:
            rep = day["rep"]
            dt = datetime.fromtimestamp(rep["dt"])
            sr, ss = self.local_sun_times(lat, lon, tz_offset, dt.date())
            description = rep["weather"][0]["description"]

            row = MDCard(
                orientation="horizontal",
                size_hint_y=None,
                height=dp(92),
                padding=[dp(14), dp(10)],
                spacing=dp(10),
                radius=[dp(14),],
                elevation=0,
                md_bg_color=(0.11, 0.16, 0.11, 1),
                line_color=(0.2, 0.4, 0.2, 1),
            )
            row.add_widget(MDIcon(
                icon=self.get_weather_icon(description),
                font_size="30sp",
                theme_text_color="Custom",
                text_color=(0.75, 1, 0.75, 1),
            ))

            col = BoxLayout(orientation="vertical", spacing=dp(4))
            col.add_widget(MDLabel(
                text=(
                    f"{dt.strftime('%d.%m')}  |  "
                    f"{int(day['tmax'])}° / {int(day['tmin'])}°"
                    f"  ·  {description.capitalize()}"
                ),
                font_size=sp(19),
                bold=True,
                theme_text_color="Custom",
                text_color=(0.9, 1, 0.9, 1),
                halign="left",
            ))
            sun_box = BoxLayout(
                orientation="horizontal",
                size_hint_y=None,
                height=dp(28),
                spacing=dp(6),
            )
            sun_box.add_widget(MDIcon(
                icon="weather-sunset-up",
                font_size="22sp",
                theme_text_color="Custom",
                text_color=(0.7, 0.95, 0.7, 1),
            ))
            sun_box.add_widget(MDLabel(
                text=sr,
                size_hint_x=None,
                width=dp(54),
                font_size=sp(15),
                theme_text_color="Custom",
                text_color=(0.85, 1, 0.85, 1),
            ))
            sun_box.add_widget(Widget(size_hint_x=None, width=dp(10)))
            sun_box.add_widget(MDIcon(
                icon="weather-sunset-down",
                font_size="22sp",
                theme_text_color="Custom",
                text_color=(0.7, 0.95, 0.7, 1),
            ))
            sun_box.add_widget(MDLabel(
                text=ss,
                size_hint_x=None,
                width=dp(54),
                font_size=sp(15),
                theme_text_color="Custom",
                text_color=(0.85, 1, 0.85, 1),
            ))
            col.add_widget(sun_box)
            row.add_widget(col)
            self.root.ids.data_list.add_widget(row)
        except Exception:
            pass

    def on_start(self):
        Clock.schedule_once(self.delayed_init, 2)


if __name__ == "__main__":
    WildVantage().run()