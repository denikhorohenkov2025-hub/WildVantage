package org.wildvantage;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Calendar;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/**
 * Погодные уведомления из фонового воркера (работают при закрытом приложении).
 *
 * Правила ЗЕРКАЛЯТ services/alerts.py (единственный источник правил —
 * Python-модуль; совпадение порогов проверяется тестом). Java дублирует их,
 * потому что при закрытом приложении Python-процесс не запущен.
 *
 * Анализируем свежий ответ MET (./raw из WeatherUpdateWorker): дождь/снег/
 * гроза по символам ближайших часов, сильный ветер/порывы, резкие скачки
 * температуры, экстремальная жара/мороз, резкий перепад давления. Уведомление
 * шлётся через NotificationManager + NotificationChannel (Android 8+);
 * тап открывает приложение (PendingIntent на launch intent).
 *
 * Тихий режим (23..7 по ЛОКАЛЬНОМУ времени устройства) подавляет некритичные
 * типы. На Android 13+ пост пропускается без разрешения POST_NOTIFICATIONS.
 * Cooldown 3 часа на тип — один и тот же тип не дублируется каждые 15 минут.
 * Старые данные не «выдаются за свежие»: уведомления считаются только из
 * только что успешно скачанного ответа (raw этого воркера).
 */
public final class WeatherNotifier {

    private static final String TAG = "WildVantage";
    private static final String CHANNEL_ID = "wv_weather_alerts";
    private static final String CHANNEL_NAME = "Погодные предупреждения";
    private static final String SETTINGS_FILE = "wildvantage_notify.json";
    private static final String PREFS = "wv_notify_cooldown";
    private static final String PREFS_LAST = "last_epoch_";
    private static final long COOLDOWN_SECONDS = 3L * 3600L;

    // Пороги — зеркало services/alerts.py.
    private static final double WIND_ALERT_MPS = 12.0;
    private static final double GUST_ALERT_MPS = 15.0;
    private static final double TEMP_SHIFT_C = 6.0;
    private static final double HEAT_C = 30.0;
    private static final double FROST_C = -25.0;
    private static final double PRESSURE_SHIFT_HPA = 8.0;

    private static final String RAIN = "rain";
    private static final String SNOW = "snow";
    private static final String THUNDER = "thunder";
    private static final String WIND = "wind";
    private static final String TEMP_DROP = "temp_drop";
    private static final String TEMP_RISE = "temp_rise";
    private static final String HEAT = "heat";
    private static final String FROST = "frost";
    private static final String PRESSURE = "pressure";

    private static final String CATEGORY_TEMP = "temperature";
    private static final String CATEGORY_PRESSURE = "pressure";

    private WeatherNotifier() {
    }

    /** Проанализировать только что скачанный ответ и разослать уведомления. */
    public static void dispatch(Context context, File appDir, String rawJson) {
        try {
            if (rawJson == null || rawJson.trim().isEmpty()) {
                return;
            }
            if (!settingsEnabled(appDir)) {
                Log.i(TAG, "notify: отключено в настройках");
                return;
            }
            if (Build.VERSION.SDK_INT >= 33
                    && context.checkSelfPermission(
                            "android.permission.POST_NOTIFICATIONS")
                    != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                Log.i(TAG, "notify: нет разрешения POST_NOTIFICATIONS");
                return;
            }
            List<Alert> alerts = evaluate(new JSONObject(rawJson));
            if (alerts.isEmpty()) {
                Log.i(TAG, "notify: ничего не сработало");
                return;
            }
            SharedPreferences cooldown = context.getSharedPreferences(
                    PREFS, Context.MODE_PRIVATE);
            long nowEpoch = System.currentTimeMillis() / 1000L;
            int hour = Calendar.getInstance().get(Calendar.HOUR_OF_DAY);
            boolean quiet = isQuiet(appDir, hour);
            List<Alert> toPost = new ArrayList<>();
            for (Alert a : alerts) {
                if (quiet && !a.critical) {
                    continue;
                }
                long last = cooldown.getLong(PREFS_LAST + a.type, 0L);
                if (nowEpoch - last < COOLDOWN_SECONDS) {
                    continue;
                }
                toPost.add(a);
                cooldown.edit().putLong(PREFS_LAST + a.type, nowEpoch).apply();
            }
            if (toPost.isEmpty()) {
                Log.i(TAG, "notify: всё в cooldown/тихом режиме");
                return;
            }
            NotificationManager nm =
                    (NotificationManager) context.getSystemService(
                            Context.NOTIFICATION_SERVICE);
            if (nm == null) {
                return;
            }
            ensureChannel(nm);
            for (Alert a : toPost) {
                Notification n = buildNotification(context, a.title, a.body);
                try {
                    nm.notify(a.type.hashCode(), n);
                    Log.i(TAG, "notify: отправлено " + a.type);
                } catch (Throwable t) {
                    Log.w(TAG, "notify: НЕ отправлено " + a.type + ": " + t);
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "notify: ошибка " + t);
        }
    }

    private static boolean settingsEnabled(File appDir) {
        try {
            File f = new File(appDir, SETTINGS_FILE);
            if (!f.isFile()) {
                return false; // по умолчанию уведомления выключены
            }
            byte[] bytes = readCapped(f);
            JSONObject s = new JSONObject(new String(bytes, StandardCharsets.UTF_8));
            return s.optBoolean("enabled", false);
        } catch (Throwable t) {
            return false;
        }
    }

    private static boolean isQuiet(File appDir, int hour) {
        try {
            File f = new File(appDir, SETTINGS_FILE);
            if (!f.isFile()) {
                return false;
            }
            byte[] bytes = readCapped(f);
            JSONObject s = new JSONObject(new String(bytes, StandardCharsets.UTF_8));
            if (!s.optBoolean("quiet", false)) {
                return false;
            }
            return hour >= 23 || hour < 7;
        } catch (Throwable t) {
            return false;
        }
    }

    private static void ensureChannel(NotificationManager nm) {
        if (Build.VERSION.SDK_INT < 26) {
            return;
        }
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, CHANNEL_NAME,
                NotificationManager.IMPORTANCE_HIGH);
        ch.setDescription("Оповещения о дожде, снеге, грозе, ветре, "
                + "скачках температуры и давления");
        nm.createNotificationChannel(ch);
    }

    private static Notification buildNotification(Context context,
                                                  String title, String body) {
        Intent launch = context.getPackageManager()
                .getLaunchIntentForPackage(context.getPackageName());
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) {
            flags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pi = PendingIntent.getActivity(
                context, 0, launch, flags);
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(context, CHANNEL_ID);
        } else {
            //noinspection deprecation
            b = new Notification.Builder(context);
        }
        b.setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(pi)
                .setAutoCancel(true)
                .setCategory(Notification.CATEGORY_ALARM)
                .setWhen(System.currentTimeMillis());
        return b.build();
    }

    // --- Анализ прогноза (зеркало evaluate_alerts) -------------------------

    private static final class Alert {
        final String type;
        final boolean critical;
        final String title;
        final String body;

        Alert(String type, boolean critical, String title, String body) {
            this.type = type;
            this.critical = critical;
            this.title = title;
            this.body = body;
        }
    }

    private static double num(Object v, double dflt) {
        try {
            return Double.parseDouble(v.toString());
        } catch (Throwable t) {
            return dflt;
        }
    }

    private static String sym(String code) {
        return code == null ? "" : code.toLowerCase(Locale.US);
    }

    private static boolean isThunder(String c) {
        return sym(c).contains("thunder");
    }

    private static boolean isSnow(String c) {
        String s = sym(c);
        return s.contains("snow") || s.contains("sleet") || s.contains("graupel");
    }

    private static boolean isRain(String c) {
        String s = sym(c);
        return s.contains("rain") && !isSnow(c);
    }

    private static JSONObject details(JSONObject entry) {
        try {
            return entry.getJSONObject("data").getJSONObject("instant")
                    .getJSONObject("details");
        } catch (Throwable t) {
            return null;
        }
    }

    private static String symbolOf(JSONObject entry) {
        try {
            JSONObject data = entry.getJSONObject("data");
            for (String key : new String[]{"next_1_hours", "next_6_hours",
                    "next_12_hours"}) {
                if (data.has(key)) {
                    String s = data.getJSONObject(key).getJSONObject("summary")
                            .optString("symbol_code", null);
                    if (s != null) {
                        return s;
                    }
                }
            }
        } catch (Throwable ignored) {
        }
        return null;
    }

    private static boolean windowHas(List<JSONObject> entries, long nowMs,
                                     long spanMs, Checker chk) {
        for (JSONObject e : entries) {
            long tMs = entryMs(e);
            if (tMs <= nowMs + spanMs) {
                String s = symbolOf(e);
                if (s != null && chk.test(s)) {
                    return true;
                }
            }
        }
        return false;
    }

    private interface Checker {
        boolean test(String code);
    }

    private static long entryMs(JSONObject entry) {
        try {
            String iso = entry.optString("time", null);
            if (iso == null) {
                return Long.MAX_VALUE;
            }
            iso = iso.endsWith("Z") ? iso : iso + "Z";
            return new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss'Z'", Locale.US)
                    .parse(iso).getTime();
        } catch (Throwable t) {
            return Long.MAX_VALUE;
        }
    }

    private static List<JSONObject> sortedEntries(JSONObject raw) {
        List<JSONObject> list = new ArrayList<>();
        try {
            JSONArray ts = raw.getJSONObject("properties").getJSONArray("timeseries");
            for (int i = 0; i < ts.length(); i++) {
                Object o = ts.get(i);
                if (o instanceof JSONObject) {
                    list.add((JSONObject) o);
                }
            }
        } catch (Throwable t) {
            return list;
        }
        list.sort((a, b) -> Long.compare(entryMs(a), entryMs(b)));
        return list;
    }

    private static double bestTemp(List<JSONObject> entries, long nowMs,
                                   long spanMs, boolean max) {
        Double best = null;
        for (JSONObject e : entries) {
            if (entryMs(e) > nowMs + spanMs) {
                break;
            }
            JSONObject d = details(e);
            if (d == null) {
                continue;
            }
            double t = num(d.opt("air_temperature"), Double.NaN);
            if (Double.isNaN(t)) {
                continue;
            }
            if (best == null || (max ? t > best : t < best)) {
                best = t;
            }
        }
        return best == null ? Double.NaN : best;
    }

    private static final long SIX_H = 6L * 3600L * 1000L;
    private static final long DAY_H = 24L * 3600L * 1000L;

    static List<Alert> evaluate(JSONObject raw) {
        List<Alert> out = new ArrayList<>();
        List<JSONObject> entries = sortedEntries(raw);
        if (entries.isEmpty()) {
            return out;
        }
        long nowMs = System.currentTimeMillis();
        JSONObject cur = entries.get(0);
        long curMs = entryMs(cur);
        for (JSONObject e : entries) {
            if (entryMs(e) <= nowMs) {
                cur = e;
                curMs = entryMs(e);
            } else {
                break;
            }
        }
        JSONObject curDet = details(cur);

        // Дождь/снег/гроза — символы текущего и ближайших часов.
        boolean thunder = isThunder(symbolOf(cur));
        boolean snow = isSnow(symbolOf(cur));
        boolean rain = isRain(symbolOf(cur));
        if (!thunder) {
            thunder = windowHas(entries, nowMs, SIX_H, WeatherNotifier::isThunder);
        }
        if (!snow) {
            snow = windowHas(entries, nowMs, SIX_H, WeatherNotifier::isSnow);
        }
        if (!rain) {
            rain = windowHas(entries, nowMs, SIX_H, WeatherNotifier::isRain);
        }
        if (thunder) {
            out.add(new Alert(THUNDER, true, "Гроза",
                    "В ближайшие часы возможна гроза."));
        }
        if (snow) {
            out.add(new Alert(SNOW, false, "Снегопад",
                    "В ближайшие часы возможен снегопад."));
        } else if (rain) {
            out.add(new Alert(RAIN, false, "Дождь",
                    "В ближайшие часы возможен дождь."));
        }

        // Ветер и порывы.
        double windMax = 0.0;
        double gustMax = curDet == null ? 0.0
                : num(curDet.opt("wind_speed_of_gust"), 0.0);
        for (JSONObject e : entries) {
            if (entryMs(e) > nowMs + SIX_H) {
                break;
            }
            JSONObject d = details(e);
            if (d == null) {
                continue;
            }
            windMax = Math.max(windMax, num(d.opt("wind_speed"), 0.0));
            gustMax = Math.max(gustMax, num(d.opt("wind_speed_of_gust"), 0.0));
        }
        if (windMax >= WIND_ALERT_MPS || gustMax >= GUST_ALERT_MPS) {
            out.add(new Alert(WIND, false, "Сильный ветер",
                    "Ожидается усиление ветра."));
        }

        // Температурные скачки / экстремумы.
        double curT = curDet == null ? Double.NaN
                : num(curDet.opt("air_temperature"), Double.NaN);
        double tMin6 = bestTemp(entries, nowMs, SIX_H, false);
        double tMax6 = bestTemp(entries, nowMs, SIX_H, true);
        double tMin24 = bestTemp(entries, nowMs, DAY_H, false);
        double tMax24 = bestTemp(entries, nowMs, DAY_H, true);
        if (!Double.isNaN(curT)) {
            if (!Double.isNaN(tMin6) && tMin6 <= curT - TEMP_SHIFT_C) {
                out.add(new Alert(TEMP_DROP, false, "Резкое похолодание",
                        "В ближайшие часы возможно резкое похолодание."));
            }
            if (!Double.isNaN(tMax6) && tMax6 >= curT + TEMP_SHIFT_C) {
                out.add(new Alert(TEMP_RISE, false, "Резкое потепление",
                        "В ближайшие часы возможно резкое потепление."));
            }
        }
        double lo = Double.isNaN(tMin24) ? curT : Math.min(tMin24, curT);
        double hi = Double.isNaN(tMax24) ? curT : Math.max(tMax24, curT);
        if (!Double.isNaN(lo) && lo <= FROST_C) {
            out.add(new Alert(FROST, true, "Сильный мороз",
                    "Ожидается сильный мороз."));
        }
        if (!Double.isNaN(hi) && hi >= HEAT_C) {
            out.add(new Alert(HEAT, true, "Экстремальная жара",
                    "Ожидается сильная жара."));
        }

        // Перепад давления: текущее против точки через ~3 часа.
        double pNow = curDet == null ? Double.NaN
                : num(curDet.opt("air_pressure_at_sea_level"), Double.NaN);
        JSONObject p3 = null;
        for (JSONObject e : entries) {
            if (entryMs(e) - curMs >= 3L * 3600L * 1000L) {
                p3 = e;
                break;
            }
        }
        if (!Double.isNaN(pNow) && p3 != null) {
            JSONObject d3 = details(p3);
            if (d3 != null) {
                double pF = num(d3.opt("air_pressure_at_sea_level"), Double.NaN);
                if (!Double.isNaN(pF)
                        && Math.abs(pF - pNow) >= PRESSURE_SHIFT_HPA) {
                    out.add(new Alert(PRESSURE, false, "Резкий перепад давления",
                            "В ближайшие часы возможно резкое изменение давления."));
                }
            }
        }
        return out;
    }

    private static byte[] readCapped(File f) throws IOException {
        java.io.FileInputStream in = new java.io.FileInputStream(f);
        try {
            java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream(4096);
            byte[] chunk = new byte[4096];
            int n;
            while ((n = in.read(chunk)) != -1) {
                buf.write(chunk, 0, n);
            }
            return buf.toByteArray();
        } finally {
            in.close();
        }
    }
}