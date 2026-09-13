import json
import math
from datetime import datetime

from kivy.clock import Clock
from kivy.lang import Builder
from kivy.utils import platform
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDIconButton, MDFillRoundFlatButton
from kivymd.uix.card import MDCard
from kivymd.uix.label import MDLabel
from kivymd.uix.list import MDList, ThreeLineIconListItem, IconLeftWidget
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.selectioncontrol import MDSwitch
from kivymd.uix.textfield import MDTextField

try:
    import requests
except Exception:
    requests = None

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
                height: dp(310)
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
                    theme_text_color: "Custom"
                    text_color: 0.9, 1, 0.9, 1
                MDLabel:
                    id: azimuth_label
                    text: "Азимут: --"
                    halign: "center"
                    font_size: sp(16)
                    theme_text_color: "Custom"
                    text_color: 0.6, 0.8, 0.6, 1
                MDLabel:
                    id: temp_label
                    text: "--°C"
                    halign: "center"
                    font_style: "H2"
                    bold: True
                    theme_text_color: "Custom"
                    text_color: 0.95, 1, 0.95, 1
                MDBoxLayout:
                    orientation: "horizontal"
                    size_hint_y: None
                    height: dp(32)
                    Widget:
                    MDIcon:
                        icon: "weather-sunset-up"
                        font_size: sp(22)
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
                    MDIcon:
                        icon: "weather-sunset-down"
                        font_size: sp(22)
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

        return Builder.load_string(KV)

    # --- Safe Boot: каркас UI уже показан, датчики/разрешения — через 2 сек ---
    def delayed_init(self, *args):
        self._set_status("Проверка датчиков и разрешений...")
        self._import_sensors()
        self._request_permissions()
        self.start_sensors()
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

    def _no_compass(self):
        try:
            self.root.ids.azimuth_label.text = "Азимут: --"
        except Exception:
            pass

    def update_compass(self, dt):
        if compass is None:
            return
        try:
            val = compass.field
            if not val:
                return
            bearing = (math.degrees(math.atan2(val[1], val[0])) + 360) % 360
            idx = int((bearing + 22.5) // 45) % 8
            self.root.ids.azimuth_label.text = (
                f"Азимут: {int(bearing)}° ({DIRECTIONS[idx]})"
            )
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
                root.search_field.disabled = True
            else:
                root.mode_text.text = "РЕЖИМ ГОРОД (ONLINE)"
                root.mode_text.text_color = 0.85, 1, 0.85, 1
                root.header.md_bg_color = 0.08, 0.12, 0.08, 1
                root.search_field.disabled = False
        except Exception:
            pass

    # --- GPS ---
    def start_gps(self, *args):
        self._set_status("Поиск спутников...")
        if gps is None:
            self._set_status("GPS недоступен")
            return
        try:
            gps.configure(on_location=self.on_location)
            gps.start()
        except Exception:
            self._set_status("Ошибка GPS: включите спутники")

    def on_location(self, **kwargs):
        self.current_lat = kwargs.get("lat")
        self.current_lon = kwargs.get("lon")
        try:
            gps.stop()
        except Exception:
            pass
        if self.is_wilderness:
            self.load_from_cache(new_lat=self.current_lat, new_lon=self.current_lon)
        else:
            self.fetch_weather_by_coords(self.current_lat, self.current_lon)

    # --- ПОИСК ГОРОДА (онлайн, с кириллицей) ---
    def search_city(self, *args):
        if self.is_wilderness:
            return
        city = self.root.ids.search_field.text.strip()
        if not city:
            self._set_status("Введите город")
            return
        if requests is None:
            self._set_status("Нет сети — данные из кэша")
            self.load_from_cache()
            return
        self._set_status("Поиск города...")
        try:
            from urllib.parse import quote

            geo_url = (
                f"https://api.openweathermap.org/geo/1.0/direct"
                f"?q={quote(city)}&limit=1&appid={self.api_key}"
            )
            r = requests.get(geo_url, timeout=5)
            if r.status_code == 401:
                self._set_status("Ошибка ключа (401) — данные из кэша")
                self.load_from_cache()
                return
            if r.status_code == 200 and r.json():
                loc = r.json()[0]
                self.current_lat = loc["lat"]
                self.current_lon = loc["lon"]
                self.fetch_weather_by_coords(loc["lat"], loc["lon"])
            else:
                self._set_status("Город не найден")
        except Exception:
            self._set_status("Нет сети — данные из кэша")
            self.load_from_cache()

    # --- ПОГОДА (онлайн) ---
    def fetch_weather_by_coords(self, lat, lon):
        if requests is None:
            self._set_status("Нет сети — данные из кэша")
            self.load_from_cache()
            return
        try:
            url = (
                f"https://api.openweathermap.org/data/2.5/forecast"
                f"?lat={lat}&lon={lon}&appid={self.api_key}&units=metric&lang=ru"
            )
            r = requests.get(url, timeout=5)
            if r.status_code == 401:
                self._set_status("Ошибка ключа (401) — данные из кэша")
                self.load_from_cache()
                return
            if r.status_code == 200:
                data = r.json()
                data["saved_at"] = datetime.now().strftime("%d.%m %H:%M")
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f)
                self.refresh_ui(data)
            else:
                self._set_status("API ошибка — данные из кэша")
                self.load_from_cache()
        except Exception:
            self._set_status("Нет сети — данные из кэша")
            self.load_from_cache()

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

    # --- ДИНАМИЧЕСКАЯ ИКОНКА ПОГОДЫ ---
    def weather_icon(self, description):
        d = description.lower()
        if any(w in d for w in ("гроза", "storm", "lightning")):
            return "weather-lightning"
        if any(w in d for w in ("снег", "snow")):
            return "weather-snowy"
        if any(w in d for w in ("дождь", "ливень", "rain", "drizzle")):
            return "weather-rainy"
        if any(w in d for w in ("туман", "дымка", "fog", "mist", "пыль", "haze")):
            return "weather-fog"
        if any(w in d for w in ("облач", "cloud")):
            return "weather-cloudy"
        return "weather-sunny"

    # --- ФИЛЬТР: ОДНА ЗАПИСЬ НА 12:00 КАЖДОГО ДНЯ ---
    def _nearest_noon(self, forecast_list):
        daily = {}
        for f in forecast_list:
            dt = datetime.fromtimestamp(f["dt"])
            key = dt.date()
            diff = abs(dt.hour - 12)
            if key not in daily or diff < daily[key][1]:
                daily[key] = (f, diff)
        return [v[0] for v in daily.values()]

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
            forecasts = self._nearest_noon(data["list"])
            self.root.ids.temp_label.text = f"{int(forecasts[0]['main']['temp'])}°C"
            self._set_status(
                f"Обновлено: {data.get('saved_at', 'Неизвестно')}"
                + ("  (кэш)" if cache else "")
            )

            today = datetime.now().date()
            sunrise, sunset = self.local_sun_times(lat, lon, tz_offset, today)
            self.root.ids.sr_label.text = sunrise
            self.root.ids.ss_label.text = sunset

            for f in forecasts:
                dt = datetime.fromtimestamp(f["dt"])
                sr, ss = self.local_sun_times(lat, lon, tz_offset, dt.date())
                description = f["weather"][0]["description"]

                item = ThreeLineIconListItem(
                    text=f"{dt.strftime('%d.%m')} | {int(f['main']['temp'])}°C",
                    secondary_text=description.capitalize(),
                    tertiary_text=f"Восход: {sr}  |  Закат: {ss}",
                )
                item.add_widget(IconLeftWidget(icon=self.weather_icon(description)))
                self.root.ids.data_list.add_widget(item)
        except Exception:
            self._set_status("Ошибка отображения данных")
            self.load_from_cache()

    def on_start(self):
        Clock.schedule_once(self.delayed_init, 2)


if __name__ == "__main__":
    WildVantage().run()