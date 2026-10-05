package org.wildvantage;

import android.content.Context;
import android.util.Log;

import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;

import java.util.concurrent.TimeUnit;

/**
 * Регистрация периодического фонового обновления погоды в WorkManager.
 *
 * Период 15 минут — минимально допустимый интервал PeriodicWorkRequest
 * (WorkManager принудительно округляет периоды меньше 15 минут вверх).
 * Задача ставится только при наличии сети; KEEP делает повторный вызов
 * идемпотентным — перезапуски приложения не создают вторую задачу.
 * Задача переживает перезагрузку (WorkManager/JobScheduler восстанавливают
 * её системой) и не требует уведомлений или foreground-сервиса.
 *
 * Doze/энергосбережение могут задерживать фактическое выполнение до окна
 * активности — это штатное поведение Android, период остаётся ~15 минут.
 */
public final class BgScheduler {

    private static final String TAG = "WildVantage";
    private static final String WORK_NAME = "wv_weather_update";
    private static final long PERIOD_MINUTES = 15;

    private BgScheduler() {
    }

    /** true — расписание поставлено (или уже стоит); false — ошибка (см. logcat). */
    public static boolean schedule(Context context) {
        try {
            Constraints constraints = new Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build();
            PeriodicWorkRequest request =
                    new PeriodicWorkRequest.Builder(
                            WeatherUpdateWorker.class, PERIOD_MINUTES, TimeUnit.MINUTES)
                            .setConstraints(constraints)
                            .build();
            WorkManager.getInstance(context.getApplicationContext())
                    .enqueueUniquePeriodicWork(
                            WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, request);
            Log.i(TAG, "bg schedule ok: work=" + WORK_NAME
                    + " period=" + PERIOD_MINUTES + "m unique=KEEP network=CONNECTED");
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "bg schedule failed: " + t);
            return false;
        }
    }
}
