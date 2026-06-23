package com.liskovsoft.smartyoutubetv2.common.litetube

import com.liskovsoft.sharedutils.okhttp.OkHttpManager
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Lightweight Litetube REST client. Reuses the shared OkHttp singleton so it
 * picks up whatever the system-proxy selector just wrote to http(s).proxyHost.
 */
object LitetubeApi {
    private const val TAG = "LitetubeApi"
    private val JSON = "application/json".toMediaTypeOrNull()

    fun startDevice(apiBase: String): JSONObject? = postJson(
        apiBase + "/api/devices/start",
        body = JSONObject(),
        bearer = null,
    )

    /** Long-poll up to server-side DEVICE_POLL_MAX_SEC. Returns null on timeout. */
    fun pollDevice(apiBase: String, code: String): JSONObject? = getJson(
        apiBase + "/api/devices/poll?code=$code", bearer = null,
        timeoutSec = 35, // server caps at 30s; add slack for TLS handshake
    )

    fun fetchProxyPool(apiBase: String, jwt: String): JSONArray? = getJsonArray(
        apiBase + "/api/proxy/pool", bearer = jwt, timeoutSec = 15,
    )

    // ---- low-level helpers ---------------------------------------------

    private fun getJson(url: String, bearer: String?, timeoutSec: Long): JSONObject? {
        val req = baseRequest(url, bearer).get().build()
        return runCatching {
            OkHttpManager.instance().okHttpClient.newBuilder()
                .callTimeout(timeoutSec, TimeUnit.SECONDS)
                .readTimeout(timeoutSec, TimeUnit.SECONDS)
                .build()
                .newCall(req).execute().use { resp ->
                    val body = resp.body?.string().orEmpty()
                    if (!resp.isSuccessful) null else JSONObject(body)
                }
        }.getOrNull()
    }

    private fun getJsonArray(url: String, bearer: String?, timeoutSec: Long): JSONArray? {
        val req = baseRequest(url, bearer).get().build()
        return runCatching {
            OkHttpManager.instance().okHttpClient.newBuilder()
                .callTimeout(timeoutSec, TimeUnit.SECONDS)
                .readTimeout(timeoutSec, TimeUnit.SECONDS)
                .build()
                .newCall(req).execute().use { resp ->
                    val body = resp.body?.string().orEmpty()
                    if (!resp.isSuccessful) null else JSONArray(body)
                }
        }.getOrNull()
    }

    private fun postJson(url: String, body: JSONObject, bearer: String?): JSONObject? {
        val req = baseRequest(url, bearer)
            .post(body.toString().toRequestBody(JSON))
            .build()
        return runCatching {
            OkHttpManager.instance().okHttpClient.newCall(req).execute().use { resp ->
                val txt = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) null else JSONObject(txt)
            }
        }.getOrNull()
    }

    private fun baseRequest(url: String, bearer: String?): Request.Builder {
        val b = Request.Builder().url(url)
            .header("Accept", "application/json")
            .header("User-Agent", "LitetubeTV/0.1 AndroidTV")
        if (!bearer.isNullOrBlank()) b.header("Authorization", bearer)
        return b
    }
}
