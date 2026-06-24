package com.liskovsoft.smartyoutubetv2.common.litetube;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.concurrent.TimeUnit;

/**
 * Lightweight Litetube REST client. Uses its own OkHttp singleton.
 */
public final class LitetubeApi {
    private static final MediaType JSON = MediaType.parse("application/json");

    private static final OkHttpClient client = new OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .build();

    private LitetubeApi() {}

    public static JSONObject startDevice(String apiBase) {
        return postJson(apiBase + "/api/devices/start", new JSONObject(), null);
    }

    public static JSONObject pollDevice(String apiBase, String code) {
        return getJson(apiBase + "/api/devices/poll?code=" + code, null, 35);
    }

    public static JSONArray fetchProxyPool(String apiBase, String jwt) {
        return getJsonArray(apiBase + "/api/proxy/pool", jwt, 15);
    }

    private static JSONObject getJson(String url, String bearer, long timeoutSec) {
        try {
            Request req = baseRequest(url, bearer).get().build();
            OkHttpClient timeoutClient = client.newBuilder()
                    .callTimeout(timeoutSec, TimeUnit.SECONDS)
                    .readTimeout(timeoutSec, TimeUnit.SECONDS)
                    .build();
            Response resp = timeoutClient.newCall(req).execute();
            try {
                if (!resp.isSuccessful()) return null;
                String body = resp.body() != null ? resp.body().string() : "";
                return new JSONObject(body);
            } finally {
                resp.close();
            }
        } catch (Exception e) {
            return null;
        }
    }

    private static JSONArray getJsonArray(String url, String bearer, long timeoutSec) {
        try {
            Request req = baseRequest(url, bearer).get().build();
            OkHttpClient timeoutClient = client.newBuilder()
                    .callTimeout(timeoutSec, TimeUnit.SECONDS)
                    .readTimeout(timeoutSec, TimeUnit.SECONDS)
                    .build();
            Response resp = timeoutClient.newCall(req).execute();
            try {
                if (!resp.isSuccessful()) return null;
                String body = resp.body() != null ? resp.body().string() : "";
                return new JSONArray(body);
            } finally {
                resp.close();
            }
        } catch (Exception e) {
            return null;
        }
    }

    private static JSONObject postJson(String url, JSONObject body, String bearer) {
        try {
            Request req = baseRequest(url, bearer)
                    .post(RequestBody.create(JSON, body.toString()))
                    .build();
            Response resp = client.newCall(req).execute();
            try {
                if (!resp.isSuccessful()) return null;
                String txt = resp.body() != null ? resp.body().string() : "";
                return new JSONObject(txt);
            } finally {
                resp.close();
            }
        } catch (Exception e) {
            return null;
        }
    }

    private static Request.Builder baseRequest(String url, String bearer) {
        Request.Builder b = new Request.Builder().url(url)
                .header("Accept", "application/json")
                .header("User-Agent", "LitetubeTV/0.1 AndroidTV");
        if (bearer != null && !bearer.isEmpty())
            b.header("Authorization", bearer);
        return b;
    }
}
