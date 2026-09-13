import json
import requests
import math
from datetime import datetime, timedelta
import pytz
from astral.sun import sun
from astral import LocationInfo

from kivymd.app import MDApp
from kivymd.uix.screen import MDScreen
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.selectioncontrol import MDSwitch
from kivymd.uix.card import MDCard
from kivymd.uix.label import MDLabel
from kivymd.uix.button import MDIconButton, MDFillRoundFlatButton
from kivymd.uix.scrollview import MDScrollView
from kivymd.uix.list import MDList, ThreeLineIconListItem, IconLeftWidget
from kivymd.uix.toolbar import MDTopAppBar

from plyer import gps, compass
from kivy.clock import Clock

class WildVantage(MDApp):
    def build(self):
        self.title = "WildVantage"
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Green"
        
        # --- НАСТРОЙКИ ---
        self.api_key = "ВСТАВЬ_СВОЙ_API_KEY_ЗДЕСЬ" 
        self.is_wilderness = False
        self.cache_file = "wildvantage_cache.json"
        
        screen = MDScreen()
        layout = MDBoxLayout(orientation='vertical')

        self.toolbar = MDTopAppBar(title="WILDVANTAGE", anchor_title="center", md_bg_color=[0.05, 0.1, 0.05, 1])
        layout.add_widget(self.toolbar)

        content = MDBoxLayout(orientation='vertical', padding=15, spacing=10)

        # Компас
        self.compass_card = MDCard(orientation='vertical', padding=10, size_hint=(1, None), height="90dp", md_bg_color=[0.1, 0.15, 0.1, 1])
        self.compass_label = MDLabel(text="КОМПАС: --°", halign="center", font_style="H5", bold=True)
        self.direction_label = MDLabel(text="Направление: --", halign="center", theme_text_color="Secondary")
        self.compass_card.add_widget(self.compass_label)
        self.compass_card.add_widget(self.direction_label)
        content.add_widget(self.compass_card)

        # Режим
        mode_box = MDBoxLayout(adaptive_height=True, spacing=10)
        self.mode_text = MDLabel(text="РЕЖИМ: ГОРОД (ONLINE)", bold=True)
        self.mode_switch = MDSwitch(active=False)
        self.mode_switch.bind(active=self.toggle_mode)
        mode_box.add_widget(self.mode_text)
        mode_box.add_widget(self.mode_switch)
        content.add_widget(mode_box)

        # Погода
        self.info_card = MDCard(orientation='vertical', padding=15, size_hint=(1, None), height="160dp", md_bg_color=[0.12, 0.18, 0.12, 1])
        self.loc_label = MDLabel(text="Ожидание данных...", halign="center", font_style="H6")
        self.temp_label = MDLabel(text="--°C", halign="center", font_style="H2", bold=True)
        self.status_label = MDLabel(text="Обновите GPS", halign="center", theme_text_color="Secondary")
        self.info_card.add_widget(self.loc_label)
        self.info_card.add_widget(self.temp_label)
        self.info_card.add_widget(self.status_label)
        content.add_widget(self.info_card)

        self.gps_btn = MDFillRoundFlatButton(text="ОБНОВИТЬ GPS КООРДИНАТЫ", pos_hint={"center_x": .5}, on_release=self.start_gps)
        content.add_widget(self.gps_btn)

        scroll = MDScrollView()
        self.data_list = MDList()
        scroll.add_widget(self.data_list)
        content.add_widget(scroll)

        layout.add_widget(content)
        screen.add_widget(layout)

        self.start_sensors()
        return screen

    def start_sensors(self):
        try:
            compass.enable()
            Clock.schedule_interval(self.update_compass, 1 / 10)
        except: self.compass_label.text = "ДАТЧИК НЕДОСТУПЕН"

    def update_compass(self, dt):
        try:
            val = compass.field
            if val:
                bearing = (math.degrees(math.atan2(val[1], val[0])) + 360) % 360
                self.compass_label.text = f"КОМПАС: {int(bearing)}°"
                dirs = ["С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ", "С"]
                self.direction_label.text = dirs[int((bearing + 22.5) / 45)]
        except: pass

    def toggle_mode(self, instance, value):
        self.is_wilderness = value
        self.mode_text.text = "РЕЖИМ: ГЛУШЬ (OFFLINE)" if value else "РЕЖИМ: ГОРОД (ONLINE)"

    def start_gps(self, *args):
        self.status_label.text = "Поиск спутников..."
        try:
            gps.configure(on_location=self.on_location)
            gps.start()
        except: self.status_label.text = "Включите GPS"

    def on_location(self, **kwargs):
        lat, lon = kwargs.get('lat'), kwargs.get('lon')
        gps.stop()
        if self.is_wilderness:
            self.load_data(lat, lon)
        else:
            url = f"https://api.openweathermap.org/data/2.5/forecast?lat={lat}&lon={lon}&appid={self.api_key}&units=metric&lang=ru"
            try:
                r = requests.get(url, timeout=5)
                data = r.json()
                data['saved_at'] = datetime.now().strftime("%H:%M")
                with open(self.cache_file, "w") as f: json.dump(data, f)
                self.refresh_ui(data)
            except: self.load_data(lat, lon)

    def load_data(self, lat, lon):
        try:
            with open(self.cache_file, "r") as f:
                data = json.load(f)
                data['city']['coord'] = {'lat': lat, 'lon': lon}
                self.refresh_ui(data, cache=True)
        except: self.status_label.text = "Кэш пуст"

    def refresh_ui(self, data, cache=False):
        self.data_list.clear_widgets()
        lat, lon = data['city']['coord']['lat'], data['city']['coord']['lon']
        # Исправляем баг часового пояса: берем сдвиг в секундах из API
        tz_offset = data['city'].get('timezone', 0) 
        
        self.loc_label.text = data['city']['name']
        forecasts = data['list'][::8]
        self.temp_label.text = f"{int(forecasts[0]['main']['temp'])}°C"
        self.status_label.text = f"Обновлено: {data.get('saved_at', 'Неизвестно')}"

        for f in forecasts:
            dt = datetime.fromtimestamp(f['dt'])
            # astral расчет
            try:
                loc = LocationInfo("", "", "UTC", lat, lon)
                s = sun(loc.observer, date=dt.date())
                # Корректируем время на часовой пояс локации
                sunrise = (s['sunrise'] + timedelta(seconds=tz_offset)).strftime('%H:%M')
                sunset = (s['sunset'] + timedelta(seconds=tz_offset)).strftime('%H:%M')
            except: sunrise = sunset = "--:--"

            item = ThreeLineIconListItem(
                text=f"{dt.strftime('%d.%m')} | {int(f['main']['temp'])}°C",
                secondary_text=f"{f['weather'][0]['description'].capitalize()}",
                tertiary_text=f"Восход: {sunrise} | Закат: {sunset}"
            )
            item.add_widget(IconLeftWidget(icon="compass-rose" if self.is_wilderness else "weather-sunny"))
            self.data_list.add_widget(item)

if __name__ == "__main__":
    WildVantage().run()