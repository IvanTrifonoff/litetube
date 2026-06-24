package com.liskovsoft.smartyoutubetv2.tv.ui.litetube;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.Gravity;
import android.view.KeyEvent;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.liskovsoft.sharedutils.mylogger.Log;
import com.liskovsoft.smartyoutubetv2.common.litetube.LitetubeApi;
import com.liskovsoft.smartyoutubetv2.common.litetube.LitetubePrefs;
import com.liskovsoft.smartyoutubetv2.tv.ui.main.SplashActivity;

import org.json.JSONObject;

import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * First-launch activation screen for Litetube TV.
 */
public class LitetubeActivationActivity extends Activity {

    private static final String TAG = "LitetubeActivation";
    private final AtomicBoolean stopFlag = new AtomicBoolean(false);
    private final java.util.concurrent.ExecutorService ioExecutor =
            Executors.newSingleThreadExecutor(r -> new Thread(r, "litetube-activation"));

    private TextView codeView;
    private TextView urlView;
    private TextView statusView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER);
        root.setPadding(48, 48, 48, 48);
        root.setBackgroundColor(0xFF0A0A14);

        TextView title = new TextView(this);
        title.setText("Активация Litetube");
        title.setTextSize(28f);
        title.setTextColor(0xFFFFFFFF);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, 32, 0, 48);
        root.addView(title);

        codeView = new TextView(this);
        codeView.setTextSize(72f);
        codeView.setTextColor(0xFF7C5CFC);
        codeView.setGravity(Gravity.CENTER);
        codeView.setPadding(0, 0, 0, 16);
        root.addView(codeView);

        urlView = new TextView(this);
        urlView.setTextSize(18f);
        urlView.setTextColor(0xFF8888AA);
        urlView.setGravity(Gravity.CENTER);
        urlView.setPadding(0, 0, 0, 32);
        root.addView(urlView);

        statusView = new TextView(this);
        statusView.setTextSize(16f);
        statusView.setTextColor(0xFFAAAAFF);
        statusView.setGravity(Gravity.CENTER);
        root.addView(statusView);

        setContentView(root);

        LitetubePrefs prefs = LitetubePrefs.instance(this);
        String api = prefs.getLitetubeApiBase();
        Log.d(TAG, "bootstrapping activation against " + api);

        ioExecutor.submit(() -> runActivationLoop(prefs, api));
    }

    private void runActivationLoop(LitetubePrefs prefs, String apiBase) {
        try {
            JSONObject start = LitetubeApi.startDevice(apiBase);
            if (start == null) {
                showFailure("Не удалось связаться с сервером активации. Проверьте интернет.");
                return;
            }
            String code = start.optString("code");
            if (code == null || code.length() != 6) {
                showFailure("Сервер вернул неожиданный ответ.");
                return;
            }
            String qrUrl = start.optString("qr_url");
            if (qrUrl == null || qrUrl.isEmpty()) {
                qrUrl = apiBase + "/activate?code=" + code;
            }
            prefs.setActivationCode(code);
            renderCode(code, qrUrl);
            showStatus("Ожидаем подтверждения на телефоне...");

            while (!stopFlag.get()) {
                JSONObject poll = LitetubeApi.pollDevice(apiBase, code);
                String status = poll != null ? poll.optString("status") : null;
                if ("claimed".equals(status)) {
                    String jwt = poll.optString("jwt");
                    if (jwt == null || jwt.isEmpty()) {
                        showFailure("Сервер не выдал токен. Попробуйте ещё раз.");
                        return;
                    }
                    prefs.setJwt(jwt);
                    prefs.clearActivationCode();
                    showStatus("Готово. Запускаем плеер...");
                    try { Thread.sleep(700); } catch (InterruptedException ignored) {}
                    proceedToSplash();
                    return;
                } else if ("expired".equals(status)) {
                    showFailure("Код истёк. Перезапустите приложение.");
                    return;
                }
                try { Thread.sleep(TimeUnit.SECONDS.toMillis(2)); } catch (InterruptedException ignored) {}
            }
        } catch (Throwable t) {
            Log.e(TAG, "activation loop crashed: " + t.getMessage(), t);
            showFailure("Ошибка: " + (t.getMessage() != null ? t.getMessage() : t.getClass().getSimpleName()));
        }
    }

    private void renderCode(String code, String url) {
        runOnUiThread(() -> {
            codeView.setText(code.substring(0, 3) + " " + code.substring(3));
            urlView.setText(url);
        });
    }

    private void showStatus(String text) {
        runOnUiThread(() -> statusView.setText(text));
    }

    private void showFailure(String text) {
        Log.w(TAG, text);
        runOnUiThread(() -> {
            statusView.setText(text);
            Toast.makeText(LitetubeActivationActivity.this, text, Toast.LENGTH_LONG).show();
        });
    }

    private void proceedToSplash() {
        runOnUiThread(() -> {
            startActivity(new Intent(LitetubeActivationActivity.this, SplashActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
            finish();
        });
    }

    @Override
    protected void onDestroy() {
        stopFlag.set(true);
        ioExecutor.shutdownNow();
        super.onDestroy();
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_DPAD_CENTER || keyCode == KeyEvent.KEYCODE_ENTER) {
            LitetubePrefs prefs = LitetubePrefs.instance(this);
            String code = prefs.getActivationCode();
            if (code != null) {
                ioExecutor.submit(() -> {
                    JSONObject poll = LitetubeApi.pollDevice(prefs.getLitetubeApiBase(), code);
                    if (poll != null) {
                        String status = poll.optString("status");
                        if ("claimed".equals(status)) {
                            String jwt = poll.optString("jwt");
                            if (jwt != null && !jwt.isEmpty()) {
                                prefs.setJwt(jwt);
                                proceedToSplash();
                            }
                        } else if ("expired".equals(status)) {
                            showFailure("Код истёк. Перезапустите приложение.");
                        }
                    }
                });
                return true;
            }
        }
        return super.onKeyDown(keyCode, event);
    }
}
