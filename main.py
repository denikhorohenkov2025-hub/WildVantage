import json
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

# Астрономия
try:
    from astral.sun import sun
    from astral import LocationInfo
    import pytz
except Exception:
    pass

API_KEY = "5dfb720a2f0c5b0c7d131f88236baecf"
CACHE_FILE = "wildvantage_v2.json"

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
            height: "315dp"
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
                theme_text_color: "Custom"
                text_color: 0.9, 1, 0.9, 1

            MDLabel:
                id: coords_label
                text: "--"
                halign: "center"
                bold: True
                font_size: "17sp"
                theme_text_color: "Custom"
                text_color: 0.7, 0.9, 0.7, 1

            MDLabel:
                id: main_temp
                text: "--°C"
                halign: "center"
                font_size: "56sp"
                bold: True
                theme_text_color: "Custom"
                text_color: 0.95, 1, 0.95, 1

            # Восход / закат — Material иконки (не эмодзи)
            MDBoxLayout:
                adaptive_height: True
                spacing: "30dp"
                pos_hint: {"center_x": .5}
                MDBoxLayout:
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
                        size_hint_x: None
                        width: self.texture_size[0]
                        text_size: None, None
                        theme_text_color: "Custom"
                        text_color: 0.85, 1, 0.85, 1
                MDBoxLayout:
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
                        size_hint_x: None
                        width: self.texture_size[0]
                        text_size: None, None
                        theme_text_color: "Custom"
                        text_color: 0.85, 1, 0.85, 1

            # Статус с иконкой
            MDBoxLayout:
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
                    size_hint_x: None
                    width: self.texture_size[0]
                    text_size: None, None
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
        self.api_key = API_KEY
        self.cache_file = CACHE_FILE
        self.is_wilderness = False
        return Builder.load_string(KV)

    def on_start(self):
        if platform == "android":
            try:
                from android.permissions import (
                    request_permissions,
                    Permission,
                )

                request_permissions(
                    [
                        Permission.ACCESS_FINE_LOCATION,
                        Permission.ACCESS_COARSE_LOCATION,
                        Permission.INTERNET,
                    ]
                )
            except Exception:
                pass
        Clock.schedule_once(self._safe_start, 2)

    def _safe_start(self, *args):
        self.load_cache()

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

    # --- ИКОНКИ ---
    def get_weather_icon(self, desc):
        d = desc.lower()
        if "гроз" in d or "шторм" in d:
            return "weather-lightning-rainy"
        if "изморось" in d or "морось" in d or "небольш" in d:
            return "weather-partly-rainy"
        if "ливень" in d or "сильн" in d or "дожд" in d:
            return "weather-pouring"
        if "снег" in d or "метель" in d or "снегопад" in d:
            return "weather-snowy-heavy"
        if "туман" in d or "дымка" in d or "мгла" in d or "пыль" in d:
            return "weather-fog"
        if "ясн" in d or "солн" in d:
            return "weather-sunny"
        if "пасмур" in d or "облач" in d or "перемен" in d:
            return "weather-cloudy"
        return "weather-sunny"

    # --- АСТРОНОМИЯ ---
    def update_sun(self, lat, lon, tz):
        try:
            loc = LocationInfo("", "", "UTC", lat, lon)
            s = sun(loc.observer, date=datetime.now().date())
            local_tz = pytz.FixedOffset(int(tz // 60)) if tz else pytz.UTC
            sr = s["sunrise"].astimezone(local_tz).strftime("%H:%M")
            ss = s["sunset"].astimezone(local_tz).strftime("%H:%M")
            self.root.ids.sunrise_label.text = sr
            self.root.ids.sunset_label.text = ss
        except Exception:
            pass

    # --- ОТОБРАЖЕНИЕ ДАННЫХ ---
    def process_data(self, data, from_cache=False):
        city_name = data.get("city", {}).get("name", "Неизвестно")
        self.root.ids.loc_label.text = city_name

        try:
            lat = data["city"]["coord"]["lat"]
            lon = data["city"]["coord"]["lon"]
            self.root.ids.coords_label.text = f"{lat:.4f}, {lon:.4f}"
        except Exception:
            lat = lon = None

        forecast_list = data.get("list")
        if not isinstance(forecast_list, list) or not forecast_list:
            self.root.ids.main_temp.text = "--°C"
            self._set_status("Прогноз недоступен")
            return

        # Группировка по дням
        days = {}
        for x in forecast_list:
            if "dt" not in x or "main" not in x:
                continue
            d = datetime.fromtimestamp(x["dt"]).date()
            days.setdefault(d, []).append(x)

        day_list = list(days.keys())[:5]  # СТРОГО 5 ДНЕЙ
        self.root.ids.forecast_list.clear_widgets()

        tz_east = data["city"].get("timezone", self.estimate_timezone(lon) if lon is not None else 0)

        for i, d_str in enumerate(day_list):
            temps = days[d_str]
            max_t = int(max(t["main"].get("temp_max", t["main"].get("temp")) for t in temps))
            min_t = int(min(t["main"].get("temp_min", t["main"].get("temp")) for t in temps))
            desc = (
                temps[len(temps) // 2].get("weather", [{}])[0]
                .get("description", "")
            )

            if i == 0:  # сегодняшний диапазон на главный экран
                self.root.ids.main_temp.text = f"{max_t}° / {min_t}°"
                self.update_sun(lat, lon, tz_east)

            item = ThreeLineIconListItem(
                text=f"{d_str.strftime('%d.%m')}  |  {max_t}° / {min_t}°",
                secondary_text=desc.capitalize(),
            )
            item.add_widget(IconLeftWidget(icon=self.get_weather_icon(desc)))
            self.root.ids.forecast_list.add_widget(item)

        if from_cache:
            self._set_status("Прогноз из кэша")
        else:
            self._set_status(f"Обновлено: {data.get('saved_at', 'Неизвестно')}")

    def estimate_timezone(self, lon):
        try:
            return int(round(lon / 15.0) * 3600)
        except Exception:
            return 0

    # --- АСИНХРОННЫЙ HTTP с таймаутом (Kivy на Android) ---
    def _fetch(self, url, on_success=None, on_error=None, timeout=None):
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
        if self.is_wilderness:  # Поиск в Глуши — по кэшу
            self.load_cache(city_filter=city)
            return
        url = (
            "https://api.openweathermap.org/data/2.5/forecast"
            f"?q={urllib_quote(city)}&appid={self.api_key}&units=metric&lang=ru"
        )
        self._fetch(url, on_success=self.on_success, on_error=self.on_error)

    # --- GPS ---
    def run_gps_logic(self):
        self._set_status("Поиск спутников...")
        if gps is None:
            self._set_status("GPS недоступен")
            return
        try:
            gps.configure(on_location=self.on_gps_loc)
            gps.start()
        except Exception:
            self._set_status("Ошибка GPS: включите спутники")

    def on_gps_loc(self, **kwargs):
        lat = kwargs.get("lat")
        lon = kwargs.get("lon")
        if lat is None or lon is None:
            self._set_status("GPS: координаты не получены")
            return
        try:
            gps.stop()
        except Exception:
            pass
        self._show_offline_point(lat, lon)
        # Гибрид: сетевой запрос с таймаутом 3 сек.
        # Есть сеть -> онлайн; нет -> кэш + фраза "⚠️ Нет сети..."
        timeout = 3 if self.is_wilderness else None
        self.fetch_forecast(lat, lon, timeout=timeout, hybrid_cache=self.is_wilderness)

    def _show_offline_point(self, lat, lon):
        try:
            self.root.ids.coords_label.text = f"{lat:.4f}, {lon:.4f}"
            self.root.ids.loc_label.text = "Точка GPS"
            self.root.ids.forecast_list.clear_widgets()
            self.update_sun(lat, lon, self.estimate_timezone(lon))
        except Exception:
            pass

    def fetch_forecast(self, lat, lon, timeout=None, hybrid_cache=False):
        url = (
            "https://api.openweathermap.org/data/2.5/forecast"
            f"?lat={lat}&lon={lon}&appid={self.api_key}&units=metric&lang=ru"
        )

        def success(_req, result):
            if not isinstance(result, dict) or "city" not in result:
                if hybrid_cache:
                    self.load_cache(lat=lat, lon=lon, show_err=True)
                return
            result["saved_at"] = datetime.now().strftime("%d.%m %H:%M")
            try:
                with open(self.cache_file, "w") as f:
                    json.dump(result, f)
            except Exception:
                pass
            self.process_data(result)

        def error(_req, _err):
            if hybrid_cache:
                self.load_cache(lat=lat, lon=lon, show_err=True)
            else:
                self.load_cache(show_err=True)

        self._fetch(url, on_success=success, on_error=error, timeout=timeout)

    # --- КЭШ ---
    def load_cache(self, city_filter=None, lat=None, lon=None, show_err=False):
        try:
            with open(self.cache_file, "r") as f:
                data = json.load(f)
            if city_filter:
                cached = data.get("city", {}).get("name", "")
                if city_filter.lower() not in cached.lower():
                    self._set_status("Город не найден в кэше")
                    return
            if lat is not None and lon is not None:
                if "city" not in data or not isinstance(data["city"], dict):
                    data["city"] = {}
                data["city"]["coord"] = {"lat": lat, "lon": lon}
            self.process_data(data, from_cache=True)
            if show_err:
                self._set_status("⚠️ Нет сети. Прогноз из кэша.")
            elif city_filter:
                self._set_status("Офлайн: данные из кэша")
        except Exception:
            self._set_status("Кэш пуст — включите интернет")

    # --- КОЛБЭКИ ---
    def on_success(self, req, result):
        try:
            if not isinstance(result, dict) or "city" not in result:
                raise ValueError("bad data")
            result["saved_at"] = datetime.now().strftime("%d.%m %H:%M")
            with open(self.cache_file, "w") as f:
                json.dump(result, f)
            self.process_data(result)
        except Exception:
            self.load_cache(show_err=True)

    def on_error(self, *args):
        self.load_cache(show_err=True)


def urllib_quote(s):
    import urllib.parse

    return urllib.parse.quote(s)


if __name__ == "__main__":
    WildVantage().run()
