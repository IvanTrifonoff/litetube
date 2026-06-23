package com.liskovsoft.smartyoutubetv2.tv.ui.main;

import android.content.Intent;
import android.os.Bundle;
import com.liskovsoft.smartyoutubetv2.common.app.presenters.SplashPresenter;
import com.liskovsoft.smartyoutubetv2.common.app.views.SplashView;
import com.liskovsoft.smartyoutubetv2.common.litetube.LitetubePrefs;
import com.liskovsoft.smartyoutubetv2.common.misc.MotherActivity;
import com.liskovsoft.smartyoutubetv2.tv.ui.litetube.LitetubeActivationActivity;

public class SplashActivity extends MotherActivity implements SplashView {
    private static final String TAG = SplashActivity.class.getSimpleName();
    private Intent mNewIntent;
    private SplashPresenter mPresenter;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        mNewIntent = getIntent();

        // Litetube activation gate. On the stlitetube flavor, if the user has
        // never completed device pairing (no JWT in SharedPreferences), we
        // forward to the activation screen and skip the upstream presenter
        // entirely. The activation activity returns here once a JWT is written.
        if (isLitetubeFlavor() && !LitetubePrefs.instance(this).hasValidJwt()) {
            startActivity(new Intent(this, LitetubeActivationActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK));
            finish();
            return;
        }

        mPresenter = SplashPresenter.instance(this);
        mPresenter.setView(this);
        mPresenter.onViewInitialized();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);

        mNewIntent = intent;

        // If a paired-up activation screen returned with a fresh JWT, downstream
        // path now has valid auth - just continue the upstream flow.
        mPresenter.onViewInitialized();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        if (mPresenter != null) mPresenter.onViewDestroyed();
    }

    @Override
    public Intent getNewIntent() {
        return mNewIntent;
    }

    @Override
    public void finishView() {
        try {
            finish();
        } catch (NullPointerException e) {
            // NullPointerException: Attempt to invoke virtual method 'void com.android.server.wm.DisplayContent.moveStack(com.android.server.wm.TaskStack, boolean)'
            e.printStackTrace();
        }
    }

    private static boolean isLitetubeFlavor() {
        try {
            Class<?> c = Class.forName("com.liskovsoft.smartyoutubetv2.tv.BuildConfig");
            String appId = (String) c.getField("APPLICATION_ID").get(null);
            return appId != null && appId.endsWith(".litetube.tv");
        } catch (Throwable t) {
            return false;
        }
    }
}
