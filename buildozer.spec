[app]

title = WILDVANTAGE PRO

package.name = wildvantage

package.domain = org.wildvantage

source.dir = .

source.include_exts = py,png,jpg,kv,atlas,json

# Можно поднять при релизе
version = 0.1.0

requirements = python3,kivy==2.2.1,kivymd==1.1.1,requests,astral,plyer,pytz,urllib3,idna,certifi,charset-normalizer,typing_extensions,pillow

orientation = portrait

fullscreen = 0

android.permissions = INTERNET, ACCESS_NETWORK_STATE, ACCESS_FINE_LOCATION, ACCESS_COARSE_LOCATION, ACCESS_WIFI_STATE

android.api = 31

android.minapi = 21

android.archs = arm64-v8a, armeabi-v7a

android.ndk = 23b

android.accept_sdk_license = True

android.enable_androidx = True

android.features = android.hardware.sensor.compass, android.hardware.location.gps

android.logcat_filters =
    *:S
    Python:V
    pyjnius:V
    plyer:V

[buildozer]

log_level = 2

warn_on_root = 1