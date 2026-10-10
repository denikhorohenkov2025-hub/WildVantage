package org.wildvantage;

import android.content.Context;
import android.util.Log;

import androidx.work.Worker;
import androidx.work.WorkerParameters;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Периодический воркер WorkManager (~15 мин): сам делает запрос MET Norway
 * (locationforecast) и атомарно сохраняет СЫРОЙ ответ рядом с кэшем
 * (wildvantage_bg_raw_v2.json).
 *
 * Почему воркер не запускает Python-сервис (проверено по AOSP android-12):
 * обычный Context.startService() из фонового процесса бросает
 * ServiceStartNotAllowedException, пока uid приложения «idle» (а он idle уже
 * через 60 секунд после закрытия приложения: состояние job-процесса считается
 * фоновым, JobScheduler не выдаёт uid временного allowlist). Поэтому вся
 * фоновая работа выполняется здесь, внутри job'а, без сервисов и уведомлений.
 *
 * Python (при открытии/раз в 60 секунд) принимает сырой файл:
 * normalize_metno + write-if-newer атомарная запись кэша. Ошибка сети или
 * отказ валидации ничего не меняет — прежние данные и прежнее «Обновлено»
 * остаются. GPS в фоне не запрашивается: координаты берутся из кэша.
 *
 * ToS MET Norway: обязателен идентифицирующий User-Agent (без него 403),
 * координаты максимум 4 знака после запятой. Accept-Encoding не шлём —
 * ответ приходит без gzip, декодирование не нужно.
 */
public class WeatherUpdateWorker extends Worker {

    private static final String TAG = "WildVantage";
    private static final String CACHE_FILE = "wildvantage_v5.json";
    private static final String RAW_FILE = "wildvantage_bg_raw_v2.json";
    private static final String RAW_TMP = "wildvantage_bg_raw_v2.json.tmp";
    private static final long CONNECT_TIMEOUT_MS = 15_000L;
    private static final long READ_TIMEOUT_MS = 20_000L;
    private static final int MAX_BODY_BYTES = 8 * 1024 * 1024;
    private static final int MAX_FETCH_ATTEMPTS = 3;
    // complete (не compact): единственный вариант с apparent_air_temperature.
    // %.4f — требование ToS (более точные координаты -> 403).
    private static final String URL_TEMPLATE =
            "https://api.met.no/weatherapi/locationforecast/2.0/complete"
                    + "?lat=%s&lon=%s";
    private static final String MET_UA =
            "WILDVANTAGE/13.0.6 (github.com/denikhorohenkov2025-hub/WildVantage)";

    public WeatherUpdateWorker(Context context, WorkerParameters params) {
        super(context, params);
    }

    @Override
    public Result doWork() {
        long startMs = System.currentTimeMillis();
        int attempt = getRunAttemptCount();
        Log.i(TAG, "BG worker START epoch=" + startMs / 1000
                + " iso=" + isoNow() + " attempt=" + attempt);
        try {
            Context ctx = getApplicationContext();
            File dir = new File(ctx.getFilesDir(), "app");
            File cache = new File(dir, CACHE_FILE);
            if (!cache.isFile()) {
                Log.i(TAG, "BG worker SKIP reason=no-cache epoch="
                        + System.currentTimeMillis() / 1000);
                return Result.success();
            }
            // Последние ПОДТВЕРЖДЁННЫЕ координаты из кэша (GPS в фоне не нужен).
            String cacheText = readUtf8(cache);
            JSONObject record = new JSONObject(cacheText);
            JSONObject coords = record.optJSONObject("coords");
            double lat = Double.NaN;
            double lon = Double.NaN;
            if (coords != null) {
                lat = coords.optDouble("lat", Double.NaN);
                lon = coords.optDouble("lon", Double.NaN);
            }
            if (!validCoord(lat, -90.0, 90.0) || !validCoord(lon, -180.0, 180.0)) {
                Log.i(TAG, "BG worker SKIP reason=no-coords epoch="
                        + System.currentTimeMillis() / 1000);
                return Result.success();
            }
            String url = String.format(Locale.US, URL_TEMPLATE,
                    String.format(Locale.US, "%.4f", lat),
                    String.format(Locale.US, "%.4f", lon));
            byte[] body = httpGet(url);
            // Атомарная запись сырого ответа: tmp + fsync + rename.
            File tmp = new File(dir, RAW_TMP);
            File raw = new File(dir, RAW_FILE);
            FileOutputStream out = new FileOutputStream(tmp);
            try {
                out.write(body);
                out.flush();
                try {
                    out.getFD().sync();
                } catch (IOException ignored) {
                    // fsync недоступен — продолжаем, rename всё равно атомарен.
                }
            } finally {
                out.close();
            }
            if (!tmp.renameTo(raw)) {
                //noinspection ResultOfMethodCallIgnored
                raw.delete();
                if (!tmp.renameTo(raw)) {
                    throw new IOException("rename raw failed");
                }
            }
            long durMs = System.currentTimeMillis() - startMs;
            Log.i(TAG, "BG worker END epoch=" + System.currentTimeMillis() / 1000
                    + " iso=" + isoNow() + " dur_ms=" + durMs
                    + " bytes=" + body.length + " wrote=" + raw.getAbsolutePath());
            return Result.success();
        } catch (Throwable t) {
            long nowMs = System.currentTimeMillis();
            Log.w(TAG, "BG worker FAIL epoch=" + nowMs / 1000
                    + " attempt=" + attempt + " err=" + t);
            // Сеть/файлы могут сбоить — до 3 попыток с backoff, затем сдаёмся
            // до следующего периода (следующий запуск начнёт счёт заново).
            if (attempt + 1 < MAX_FETCH_ATTEMPTS) {
                return Result.retry();
            }
            Log.w(TAG, "BG worker GAVE UP attempts=" + MAX_FETCH_ATTEMPTS
                    + " epoch=" + nowMs / 1000);
            return Result.success();
        }
    }

    private static boolean validCoord(double v, double min, double max) {
        return !Double.isNaN(v) && !Double.isInfinite(v) && v >= min && v <= max;
    }

    private static byte[] httpGet(String urlStr) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) new URL(urlStr).openConnection();
        try {
            conn.setConnectTimeout((int) CONNECT_TIMEOUT_MS);
            conn.setReadTimeout((int) READ_TIMEOUT_MS);
            conn.setRequestMethod("GET");
            conn.setInstanceFollowRedirects(true);
            conn.setRequestProperty("User-Agent", MET_UA);
            conn.setRequestProperty("Accept", "application/json");
            int status = conn.getResponseCode();
            if (status != HttpURLConnection.HTTP_OK) {
                throw new IOException("http status " + status);
            }
            InputStream in = conn.getInputStream();
            try {
                return readCapped(in, MAX_BODY_BYTES);
            } finally {
                in.close();
            }
        } finally {
            conn.disconnect();
        }
    }

    private static byte[] readCapped(InputStream in, int cap) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream(64 * 1024);
        byte[] chunk = new byte[32 * 1024];
        int total = 0;
        int n;
        while ((n = in.read(chunk)) != -1) {
            total += n;
            if (total > cap) {
                throw new IOException("response too large");
            }
            buf.write(chunk, 0, n);
        }
        return buf.toByteArray();
    }

    private static String readUtf8(File f) throws IOException {
        FileInputStream in = new FileInputStream(f);
        try {
            byte[] bytes = readCapped(in, 2 * 1024 * 1024);
            return new String(bytes, StandardCharsets.UTF_8);
        } finally {
            in.close();
        }
    }

    private static String isoNow() {
        try {
            return new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ssZ", Locale.US)
                    .format(new Date());
        } catch (Throwable t) {
            return "n/a";
        }
    }
}
