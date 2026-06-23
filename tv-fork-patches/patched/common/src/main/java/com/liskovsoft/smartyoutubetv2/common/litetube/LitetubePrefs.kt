package com.liskovsoft.smartyoutubetv2.common.litetube

import android.content.Context
import android.content.SharedPreferences

/**
 * Persistent Litetube state: client JWT (post-activation), cached proxy
 * pool (last /api/proxy/pool response), and the activation code currently
 * being advertised on TV.
 *
 * Plain SharedPreferences: Android TV apps are sandboxed and rooted-TVs are
 * a separate hardening phase. Anything sensitive (JWT) gets revoked server-side
 * by /api/auth/* anyway, so on-device encryption adds little.
 */
class LitetubePrefs private constructor(private val sp: SharedPreferences) {

    fun hasValidJwt(): Boolean = !getJwt().isNullOrBlank()

    fun getJwt(): String? {
        val raw = sp.getString(KEY_JWT, null) ?: return null
        // Stored value is the raw JWT (no Bearer prefix); LitetubeApi.baseRequest
        // adds the Authorization header. Treat any non-blank value as valid -
        // server-side validation on next API call will reject forged tokens.
        return raw.takeIf { it.isNotBlank() }
    }

    fun setJwt(jwt: String) {
        sp.edit().putString(KEY_JWT, jwt).apply()
    }

    fun clearJwt() {
        sp.edit().remove(KEY_JWT).apply()
    }

    fun getActivationCode(): String? = sp.getString(KEY_ACTIVATION_CODE, null)

    fun setActivationCode(code: String) {
        sp.edit().putString(KEY_ACTIVATION_CODE, code).apply()
    }

    fun clearActivationCode() {
        sp.edit().remove(KEY_ACTIVATION_CODE).apply()
    }

    fun setCachedProxyPoolJson(json: String) {
        sp.edit().putString(KEY_CACHED_POOL, json).apply()
    }

    fun getCachedProxyPoolJson(): String? = sp.getString(KEY_CACHED_POOL, null)

    fun getLitetubeApiBase(): String =
        sp.getString(KEY_API_BASE, DEFAULT_API_BASE) ?: DEFAULT_API_BASE

    fun setLitetubeApiBase(base: String) {
        sp.edit().putString(KEY_API_BASE, base.trimEnd('/')).apply()
    }

    companion object {
        private const val PREFS = "litetube_prefs"
        private const val KEY_JWT = "jwt"
        private const val KEY_ACTIVATION_CODE = "activation_code"
        private const val KEY_CACHED_POOL = "cached_proxy_pool"
        private const val KEY_API_BASE = "api_base"

        // Default points at the public Litetube gateway; mirrors the upstream
        // upstream SmartTube build targets test servers by default.
        const val DEFAULT_API_BASE = "https://api.litetube.trfnv.ru"

        @Volatile private var instance: LitetubePrefs? = null

        @JvmStatic
        fun instance(context: Context): LitetubePrefs =
            instance ?: synchronized(this) {
                instance ?: LitetubePrefs(
                    context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                ).also { instance = it }
            }
    }
}
