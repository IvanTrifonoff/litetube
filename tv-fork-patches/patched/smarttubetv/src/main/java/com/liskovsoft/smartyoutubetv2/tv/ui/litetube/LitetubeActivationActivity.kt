package com.liskovsoft.smartyoutubetv2.tv.ui.litetube

import android.app.Activity
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Bundle
import android.view.KeyEvent
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import com.liskovsoft.sharedutils.mylogger.Log
import com.liskovsoft.smartyoutubetv2.R
import com.liskovsoft.smartyoutubetv2.common.litetube.LitetubeApi
import com.liskovsoft.smartyoutubetv2.common.litetube.LitetubePrefs
import com.liskovsoft.smartyoutubetv2.tv.ui.main.SplashActivity
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * First-launch activation screen for Litetube TV. Renders the 6-digit code +
 * QR pointing at https://litetube.trfnv.ru/activate?code=XXXXXX, then
 * long-polls /api/devices/poll. Once claimed, persists JWT and forwards
 * upstream SplashActivity to the normal launcher flow.
 *
 * Designed for leanback: title at top, big centered QR + code, instructions
 * below. Single-screen, no scrolling, no focus trap. Confirmed with the
 * D-pad: tap-pairs OK → finished.
 */
class LitetubeActivationActivity : Activity() {

    private val tag = LitetubeActivationActivity::class.java.simpleName
    private val stopFlag = AtomicBoolean(false)
    private val ioExecutor = Executors.newSingleThreadExecutor { r ->
        Thread(r, "litetube-activation").apply { isDaemon = true }
    }

    private lateinit var codeView: TextView
    private lateinit var qrView: ImageView
    private lateinit var statusView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_litetube_activation)
        codeView = findViewById(R.id.litetube_code)
        qrView = findViewById(R.id.litetube_qr)
        statusView = findViewById(R.id.litetube_status)

        val prefs = LitetubePrefs.instance(this)
        val api = prefs.getLitetubeApiBase()
        Log.d(tag, "bootstrapping activation against $api")

        ioExecutor.submit { runActivationLoop(prefs, api) }
    }

    private fun runActivationLoop(prefs: LitetubePrefs, apiBase: String) {
        try {
            val start = LitetubeApi.startDevice(apiBase)
                ?: return@runActivationLoop showFailure("Не удалось связаться с сервером активации. Проверьте интернет.")
            val code = start.optString("code").takeIf { it.length == 6 }
                ?: return@runActivationLoop showFailure("Сервер вернул неожиданный ответ.")
            val qrUrl = start.optString("qr_url")
                .ifBlank { "$apiBase/../activate?code=$code" }
            prefs.setActivationCode(code)
            renderCodeAndQr(code, qrUrl)
            showStatus("Ожидаем подтверждения на телефоне…")

            while (!stopFlag.get()) {
                val poll = LitetubeApi.pollDevice(apiBase, code)
                when (poll?.optString("status")) {
                    "claimed" -> {
                        val jwt = poll.optString("jwt")
                        if (jwt.isBlank()) {
                            showFailure("Сервер не выдал токен. Попробуйте ещё раз.")
                            return@runActivationLoop
                        }
                        prefs.setJwt(jwt)
                        prefs.clearActivationCode()
                        showStatus("Готово. Запускаем плеер…")
                        Thread.sleep(700) // let the user see the success state
                        return@runActivationLoop proceedToSplash()
                    }
                    "expired" -> {
                        showFailure("Код истёк. Перезапустите приложение.")
                        return@runActivationLoop
                    }
                    else -> Thread.sleep(TimeUnit.SECONDS.toMillis(2))
                }
            }
        } catch (t: Throwable) {
            Log.e(tag, "activation loop crashed: ${t.message}", t)
            showFailure("Ошибка: ${t.message ?: t.javaClass.simpleName}")
        }
    }

    private fun renderCodeAndQr(code: String, qrUrl: String) {
        runOnUiThread {
            codeView.text = code.chunked(3).joinToString(" ")
            qrView.setImageBitmap(buildQr(qrUrl, 720, 720))
        }
    }

    private fun showStatus(text: String) {
        runOnUiThread { statusView.text = text }
    }

    private fun showFailure(text: String) {
        Log.w(tag, text)
        runOnUiThread {
            statusView.text = text
            Toast.makeText(this, text, Toast.LENGTH_LONG).show()
        }
    }

    private fun proceedToSplash() {
        runOnUiThread {
            startActivity(Intent(this, SplashActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK))
            finish()
        }
    }

    /** ZXing is already a transitive dep via chatkit (see settings.gradle). */
    private fun buildQr(content: String, w: Int, h: Int): Bitmap {
        val hints = mapOf<EncodeHintType, Any>(EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.M)
        val matrix = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE, w, h, hints)
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
        for (x in 0 until w) {
            for (y in 0 until h) {
                bmp.setPixel(x, y, if (matrix[x, y]) Color.BLACK else Color.WHITE)
            }
        }
        return bmp
    }

    override fun onDestroy() {
        stopFlag.set(true)
        ioExecutor.shutdownNow()
        super.onDestroy()
    }

    /** D-pad ENTER on the QR triggers a manual re-poll — useful if the user
     *  completes the web step while we're between ticks. */
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if ((keyCode == KeyEvent.KEYCODE_DPAD_CENTER || keyCode == KeyEvent.KEYCODE_ENTER)
            && codeView.visibility == View.VISIBLE) {
            val code = LitetubePrefs.instance(this).getActivationCode() ?: return false
            ioExecutor.submit {
                LitetubeApi.pollDevice(LitetubePrefs.instance(this).getLitetubeApiBase(), code)
                    ?.let(::handlePollResult)
            }
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    private fun handlePollResult(p: org.json.JSONObject) {
        when (p.optString("status")) {
            "claimed" -> {
                val jwt = p.optString("jwt")
                if (jwt.isNotBlank()) {
                    LitetubePrefs.instance(this).setJwt(jwt)
                    proceedToSplash()
                }
            }
            "expired" -> showFailure("Код истёк. Перезапустите приложение.")
        }
    }
}
