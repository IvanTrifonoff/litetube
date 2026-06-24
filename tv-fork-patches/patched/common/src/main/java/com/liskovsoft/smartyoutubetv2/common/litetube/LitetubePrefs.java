package com.liskovsoft.smartyoutubetv2.common.litetube;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * Persistent Litetube state: client JWT, cached proxy pool, activation code.
 */
public class LitetubePrefs {
    private static final String PREFS = "litetube_prefs";
    private static final String KEY_JWT = "jwt";
    private static final String KEY_ACTIVATION_CODE = "activation_code";
    private static final String KEY_CACHED_POOL = "cached_proxy_pool";
    private static final String KEY_API_BASE = "api_base";

    public static final String DEFAULT_API_BASE = "https://api.litetube.trfnv.ru";

    private static volatile LitetubePrefs instance;
    private final SharedPreferences sp;

    private LitetubePrefs(SharedPreferences sp) {
        this.sp = sp;
    }

    public static LitetubePrefs instance(Context context) {
        if (instance == null) {
            synchronized (LitetubePrefs.class) {
                if (instance == null) {
                    instance = new LitetubePrefs(
                            context.getApplicationContext()
                                    .getSharedPreferences(PREFS, Context.MODE_PRIVATE));
                }
            }
        }
        return instance;
    }

    public boolean hasValidJwt() {
        String jwt = getJwt();
        return jwt != null && !jwt.isEmpty();
    }

    public String getJwt() {
        return sp.getString(KEY_JWT, null);
    }

    public void setJwt(String jwt) {
        sp.edit().putString(KEY_JWT, jwt).apply();
    }

    public void clearJwt() {
        sp.edit().remove(KEY_JWT).apply();
    }

    public String getActivationCode() {
        return sp.getString(KEY_ACTIVATION_CODE, null);
    }

    public void setActivationCode(String code) {
        sp.edit().putString(KEY_ACTIVATION_CODE, code).apply();
    }

    public void clearActivationCode() {
        sp.edit().remove(KEY_ACTIVATION_CODE).apply();
    }

    public void setCachedProxyPoolJson(String json) {
        sp.edit().putString(KEY_CACHED_POOL, json).apply();
    }

    public String getCachedProxyPoolJson() {
        return sp.getString(KEY_CACHED_POOL, null);
    }

    public String getLitetubeApiBase() {
        String base = sp.getString(KEY_API_BASE, DEFAULT_API_BASE);
        return base != null ? base : DEFAULT_API_BASE;
    }

    public void setLitetubeApiBase(String base) {
        sp.edit().putString(KEY_API_BASE, base.trim().replaceAll("/$", "")).apply();
    }
}
