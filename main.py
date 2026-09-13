import json
import math
import requests
from datetime import datetime, timedelta

import pytz
from astral import LocationInfo
from astral.sun import sun

from kivymd.app import MDApp
from kivymd.uix.screen import MDScreen
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDIconButton, MDFillRoundFlatButton
from kivymd.uix.card import MDCard
from kivymd.uix.label import MDLabel
from kivymd.uix.list import MDList, ThreeLineIconListItem, IconLeftWidget
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.selectioncontrol import MDSwitch
from kivymd.uix.textfield import MDTextField
from kivymd.uix.toolbar import MDTopAppBar

from kivy.clock import Clock
from plyer import gps, compass

API_KEY = "4f737ca86a1f055b4165360cfa41538d"  # замени на рабочий ключ OpenWeatherMap
CACHE_FILE = "wildvantage_cache.json"
DIRECTIONS = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ"]


class WildVantage(MDApp):
    def build(self):
        self.title = "WildVantage"
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Green"
        self.theme_cls.primary_hue = "900"

        self.is_wilderness = False
        self.current_lat = None
        self.current_lon = None

        screen = MDScreen()
        layout = MDBoxLayout(orientation="vertical")

        self.toolbar = MDTopAppBar(
            title="WILDVANTAGE",
            anchor_title="center",
            md_bg_color=[0.05, 0.1, 0.05, 1],
            elevation=3,
        )
        layout.add_widget(self.toolbar)

        content = MDBoxLayout(orientation="vertical", padding=15, spacing=10)

        # --- КОМПАС ---
        self.compass_card = MDCard(
            orientation="vertical",
            padding=10,
            size_hint=(1, None),
            height="90dp",
            radius=[15],
            md_bg_color=[0.1, 0.15, 0.1, 1],
        )
        self.compass_label = MDLabel(
            text="КОМПАС: --°", halign="center", font_style="H5", bold=True
        )
        self.direction_label = MDLabel(
            text="Направление: --", halign="center", theme_text_color="Secondary"
        )
        self.compass_card.add_widget(self.compass_label)
        self.compass_card.add_widget(self.direction_label)
        content.add_widget(self.compass_card)

        # --- РЕЖИМ ---
        mode_box = MDBoxLayout(adaptive_height=True, spacing=10)
        self.mode_text = MDLabel(text="РЕЖИМ: ГОРОД (ONLINE)", bold=True)
        self.mode_switch = MDSwitch(active=False)
        self.mode_switch.bind(active=self.toggle_mode)
        mode_box.add_widget(self.mode_text)
        mode_box.add_widget(self.mode_switch)
        content.add_widget(mode_box)

        # --- ПОИСК ГОРОДА (только онлайн) ---
        search_box = MDBoxLayout(adaptive_height=True, spacing=10)
        self.search_field = MDTextField(
            hint_text="Название города",
            size_hint=(1, 1),
            mode="fill",
            fill_color=[0.1, 0.15, 0.1, 1],
        )
        self.search_btn = MDIconButton(icon="magnify", on_release=self.search_city)
        search_box.add_widget(self.search_field)
        search_box.add_widget(self.search_btn)
        content.add_widget(search_box)

        # --- ПОГОДА ---
        self.info_card = MDCard(
            orientation="vertical",
            padding=15,
            size_hint=(1, None),
            height="160dp",
            radius=[20],
            md_bg_color=[0.12, 0.18, 0.12, 1],
        )
        self.loc_label = MDLabel(text="Ожидание данных...", halign="center", font_style="H6")
        self.temp_label = MDLabel(text="--°C", halign="center", font_style="H2", bold=True)
        self.status_label = MDLabel(
            text="Обновите GPS или введите город",
            halign="center",
            theme_text_color="Secondary",
        )
        self.info_card.add_widget(self.loc_label)
        self.info_card.add_widget(self.temp_label)
        self.info_card.add_widget(self.status_label)
        content.add_widget(self.info_card)

        self.gps_btn = MDFillRoundFlatButton(
            text="ОБНОВИТЬ GPS КООРДИНАТЫ",
            pos_hint={"center_x": 0.5},
            md_bg_color=[0.2, 0.4, 0.2, 1],
            on_release=self.start_gps,
        )
        content.add_widget(self.gps_btn)

        # --- ПРОГНОЗ НА 5 ДНЕЙ ---
        scroll = MDScrollView()
        self.data_list = MDList()
        scroll.add_widget(self.data_list)
        content.add_widget(scroll)

        layout.add_widget(content)
        screen.add_widget(layout)

        self.start_sensors()
        self.load_from_cache()
        return screen

    # --- КОМПАС (офлайн, магнитометр) ---
    def start_sensors(self):
        try:
            compass.enable()
            Clock.schedule_interval(self.update_compass, 1 / 10)
        except Exception:
            self.compass_label.text = "НЕТ ДАТЧИКА"
            self.direction_label.text = "Магнитометр недоступен на этом устройстве"

    def update_compass(self, dt):
        try:
            val = compass.field
            if not val:
                return
            bearing = (math.degrees(math.atan2(val[1], val[0])) + 360) % 360
            self.compass_label.text = f"КОМПАС: {int(bearing)}°"
            idx = int((bearing + 22.5) // 45) % 8
            self.direction_label.text = DIRECTIONS[idx]
        except Exception:
            pass

    # --- ПЕРЕКЛЮЧЕНИЕ РЕЖИМА ---
    def toggle_mode(self, instance, value):
        self.is_wilderness = value
        if value:
            self.mode_text.text = "РЕЖИМ: ГЛУШЬ (OFFLINE)"
            self.mode_text.theme_text_color = "Error"
            self.toolbar.md_bg_color = [0.15, 0.05, 0.05, 1]
            self.search_field.disabled = True
        else:
            self.mode_text.text = "РЕЖИМ: ГОРОД (ONLINE)"
            self.mode_text.theme_text_color = "Primary"
            self.toolbar.md_bg_color = [0.05, 0.1, 0.05, 1]
            self.search_field.disabled = False

    # --- GPS ---
    def start_gps(self, *args):
        self.status_label.text = "Поиск спутников..."
        try:
            gps.configure(on_location=self.on_location)
            gps.start()
        except Exception:
            self.status_label.text = "Ошибка GPS: включите спутники"

    def on_location(self, **kwargs):
        self.current_lat = kwargs.get("lat")
        self.current_lon = kwargs.get("lon")
        gps.stop()
        if self.is_wilderness:
            self.load_from_cache(new_lat=self.current_lat, new_lon=self.current_lon)
        else:
            self.fetch_weather_by_coords(self.current_lat, self.current_lon)

    # --- ПОИСК ГОРОДА (онлайн) ---
    def search_city(self, *args):
        if self.is_wilderness:
            return
        city = self.search_field.text.strip()
        if not city:
            self.status_label.text = "Введите название города"
            return
        self.status_label.text = "Поиск города..."
        try:
            geo_url = (
                f"https://api.openweathermap.org/geo/1.0/direct"
                f"?q={city}&limit=1&appid={API_KEY}"
            )
            r = requests.get(geo_url, timeout=5)
            if r.status_code == 401:
                self.status_label.text = "Ошибка API: неверный ключ (401)"
                return
            if r.status_code == 200 and r.json():
                loc = r.json()[0]
                self.current_lat = loc["lat"]
                self.current_lon = loc["lon"]
                self.fetch_weather_by_coords(loc["lat"], loc["lon"])
            else:
                self.status_label.text = "Город не найден"
        except Exception:
            self.status_label.text = "Нет сети — работаю из кэша"
            self.load_from_cache()

    # --- ПОГОДА (онлайн) ---
    def fetch_weather_by_coords(self, lat, lon):
        try:
            url = (
                f"https://api.openweathermap.org/data/2.5/forecast"
                f"?lat={lat}&lon={lon}&appid={API_KEY}&units=metric&lang=ru"
            )
            r = requests.get(url, timeout=5)
            if r.status_code == 401:
                self.status_label.text = "Ошибка API: неверный ключ (401)"
                return
            if r.status_code == 200:
                data = r.json()
                data["saved_at"] = datetime.now().strftime("%d.%m %H:%M")
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f)
                self.refresh_ui(data)
            else:
                self.status_label.text = "API ошибка — работаю из кэша"
                self.load_from_cache()
        except Exception:
            self.status_label.text = "Нет сети — работаю из кэша"
            self.load_from_cache()

    # --- УМНЫЙ КЭШ ---
    def load_from_cache(self, new_lat=None, new_lon=None):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            recalc_sun_for_current_pos = False
            if new_lat and new_lon:
                data["city"]["coord"] = {"lat": new_lat, "lon": new_lon}
                data["city"]["name"] = f"Точка GPS: {new_lat:.2f}, {new_lon:.2f}"
                data["city"]["timezone"] = self.estimate_timezone(new_lon)
                recalc_sun_for_current_pos = True
            self.refresh_ui(data, cache=True, recalc_sun=recalc_sun_for_current_pos)
        except Exception:
            self.status_label.text = "Нет кэша — данные недоступны"

    def estimate_timezone(self, lon):
        try:
            return int(round(lon / 15.0) * 3600)
        except Exception:
            return 0

    def local_sun_times(self, lat, lon, tz_offset, date):
        try:
            loc = LocationInfo("", "", "UTC", lat, lon)
            s = sun(loc.observer, date=date)
            local_tz = pytz.FixedOffset(int(tz_offset // 60))
            sunrise = s["sunrise"].astimezone(local_tz).strftime("%H:%M")
            sunset = s["sunset"].astimezone(local_tz).strftime("%H:%M")
            return sunrise, sunset
        except Exception:
            return "--:--", "--:--"

    # --- ОБНОВЛЕНИЕ ИНТЕРФЕЙСА ---
    def refresh_ui(self, data, cache=False, recalc_sun=False):
        self.data_list.clear_widgets()

        lat = data["city"]["coord"]["lat"]
        lon = data["city"]["coord"]["lon"]
        tz_offset = data["city"].get("timezone", self.estimate_timezone(lon))

        self.loc_label.text = data["city"]["name"]
        forecasts = data["list"][::8]
        self.temp_label.text = f"{int(forecasts[0]['main']['temp'])}°C"
        self.status_label.text = (
            f"Обновлено: {data.get('saved_at', 'Неизвестно')}"
            + ("  (кэш)" if cache else "")
        )

        for f in forecasts:
            dt = datetime.fromtimestamp(f["dt"])
            sunrise, sunset = self.local_sun_times(lat, lon, tz_offset, dt.date())

            item = ThreeLineIconListItem(
                text=f"{dt.strftime('%d.%m')} | {int(f['main']['temp'])}°C",
                secondary_text=f"{f['weather'][0]['description'].capitalize()}",
                tertiary_text=f"🌅 Восход: {sunrise} | 🌇 Закат: {sunset}",
            )
            item.add_widget(
                IconLeftWidget(
                    icon="compass-rose" if self.is_wilderness else "weather-sunny"
                )
            )
            self.data_list.add_widget(item)


if __name__ == "__main__":
    WildVantage().run()