[app]

title = WildVantage

package.name = wildvantage

package.domain = org.wildvantage

source.dir = .

source.include_exts = py,png,jpg,kv,atlas,json

# Можно поднять при релизе
version = 13.0.7

# tzdata — IANA-пояса на Android (zoneinfo без системного tzdata):
# часы прогноза и «Обновлено» считаются по поясу места.
requirements = python3,kivy==2.2.1,kivymd==1.1.1,plyer,pillow,urllib3,certifi,tzdata

orientation = portrait

fullscreen = 0

android.permissions = INTERNET, ACCESS_FINE_LOCATION, ACCESS_COARSE_LOCATION

android.api = 31

android.minapi = 21

android.archs = arm64-v8a, armeabi-v7a

android.ndk = 25b

p4a.branch = v2024.01.21

android.accept_sdk_license = True

android.enable_androidx = True

# Фоновое обновление погоды (~15 мин): WorkManager (unique periodic work)
# ставит задачу при наличии сети; Java-воркер сам берёт координаты из кэша,
# запрос MET Norway и атомарно пишет сырой ответ рядом с кэшем — Python
# принимает его при открытии/раз в 60 секунд. Без foreground-сервиса и
# уведомлений. Периодичность приблизительная (Doze откладывает до окон
# активности) — это штатное поведение системы.
android.gradle_dependencies = androidx.work:work-runtime:2.7.1

android.add_gradle_repositories = mavenCentral()

# Java-исходники воркера и планировщика (компилируются вместе с проектом).
android.add_src = src/java

android.logcat_filters =
    *:S
    Python:V
    pyjnius:V
    plyer:V
    WildVantage:V

[buildozer]

log_level = 2

warn_on_root = 1