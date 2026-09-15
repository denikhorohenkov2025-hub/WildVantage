[app]

title = WildVantage

package.name = wildvantage

package.domain = org.wildvantage

source.dir = .

source.include_exts = py,png,jpg,kv,atlas,json

# Можно поднять при релизе
version = 12.7

requirements = python3,kivy==2.2.1,kivymd==1.1.1,astral,plyer,pytz,pillow,urllib3,certifi

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

android.logcat_filters =
    *:S
    Python:V
    pyjnius:V
    plyer:V

[buildozer]

log_level = 2

warn_on_root = 1