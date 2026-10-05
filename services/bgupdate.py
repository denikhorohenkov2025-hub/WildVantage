# -*- coding: utf-8 -*-
"""Одноразовый фоновый вход: обновляет погоду по кэшу и завершается.

Запускается Android-сервисом org.wildvantage.ServiceBgupdate (не foreground:
уведомления нет; PythonService сам вызывает stopSelf() после возврата из
этого файла). Процесс сервиса отдельный и без Activity, поэтому ДО импорта
main ставим лёгкие заглушки kivy/kivymd: kivymd.app при импорте создаёт
окно, которого здесь нет, а сервису нужны только чистые функции (сеть,
нормализация ответа, атомарная запись кэша).

GPS в фоне не используется — берутся последние ПОДТВЕРЖДЁННЫЕ координаты
из кэш-файла. Нет координат или сети — файл не меняется, время
«Обновлено» в статусе не сдвигается.
"""
import os
import sys
import types


def _mk(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


def install_ui_stubs():
    """Ставит заглушки kivy/kivymd (повторный вызов безопасен)."""
    if getattr(sys.modules.get("kivymd.app"), "_wv_stub", False):
        return

    kivy = _mk("kivy")
    kivy.__path__ = []
    metrics = _mk("kivy.metrics")

    def _dp(v):
        try:
            return float(str(v).replace("dp", "").replace("sp", ""))
        except Exception:
            return v

    metrics.dp = _dp
    metrics.sp = _dp
    kivy.metrics = metrics
    clock = _mk("kivy.clock")

    class _Clock:
        @staticmethod
        def schedule_once(callback, timeout=None):
            return None

        @staticmethod
        def schedule_interval(callback, timeout=None):
            return None

        @staticmethod
        def unschedule(callback, *args, **kwargs):
            return None

    clock.Clock = _Clock
    utils = _mk("kivy.utils")
    utils.platform = "android"
    lang = _mk("kivy.lang")

    class _Builder:
        @staticmethod
        def load_string(string):
            return None

    lang.Builder = _Builder
    kivy.clock = clock
    kivy.utils = utils
    kivy.lang = lang

    kivymd = _mk("kivymd")
    kivymd.__path__ = []
    app = _mk("kivymd.app")

    class _MDApp:
        def __init__(self, *args, **kwargs):
            pass

    app.MDApp = _MDApp
    app._wv_stub = True
    uix = _mk("kivymd.uix")
    uix.__path__ = []

    class _Widget:
        def __init__(self, *args, **kwargs):
            pass

    lst = _mk("kivymd.uix.list")
    lst.ThreeLineIconListItem = _Widget
    lst.IconLeftWidget = _Widget
    uix.list = lst
    box = _mk("kivymd.uix.boxlayout")
    box.MDBoxLayout = _Widget
    uix.boxlayout = box
    fl = _mk("kivymd.uix.floatlayout")
    fl.MDFloatLayout = _Widget
    uix.floatlayout = fl
    lab = _mk("kivymd.uix.label")
    lab.MDLabel = _Widget
    lab.MDIcon = _Widget
    uix.label = lab
    kivymd.uix = uix
    kivymd.app = app


def run(cache_path=None):
    """Импортирует main (уже под стабами) и выполняет одно фоновое обновление."""
    install_ui_stubs()
    # Корень файлов приложения: на устройстве это ANDROID_ARGUMENT (filesDir/app,
    # start.c уже сделал туда chdir); в тестах — родитель каталога этого файла.
    # Кэш читаем по cwd (на устройстве cwd = app_root).
    root = os.environ.get("ANDROID_ARGUMENT") or os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    if os.environ.get("ANDROID_ARGUMENT"):
        try:
            os.chdir(root)
        except OSError:
            pass
    if root not in sys.path:
        sys.path.insert(0, root)
    import main  # noqa: E402  — только после установки стабов

    path = cache_path or main.CACHE_FILE
    ok = main.run_background_update(path)
    print("[WildVantage] background update:", "ok" if ok else "skipped")
    return ok


if __name__ == "__main__":
    run()
