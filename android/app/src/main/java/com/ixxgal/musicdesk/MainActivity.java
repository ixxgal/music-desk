package com.ixxgal.musicdesk;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * 音乐下载台 · 手机端壳。
 *
 * 两种跑法：
 *   本机模式 —— 手机里内置了 CPython（Chaquopy），后端就在手机上跑，WebView 指向 127.0.0.1。
 *              不依赖电脑，飞机上也能用。
 *   遥控模式 —— 后端跑在电脑上，这里只当遥控器（要同一个 Wi-Fi）。
 *
 * 模式来源：首次启动的引导页（assets/setup.html）里选，存 SharedPreferences。
 */
public class MainActivity extends Activity {

    private static final String PREF = "musicdesk";
    private static final String KEY_SERVER = "server";
    private static final String KEY_MODE = "mode";        // "local" | "remote"
    private static final String SETUP_URL = "file:///android_asset/setup.html";

    private WebView web;
    private SharedPreferences sp;
    private long lastBackAt = 0;
    private volatile String localUrl = "";
    private volatile String localError = "";

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        sp = getSharedPreferences(PREF, MODE_PRIVATE);

        // 状态栏跟随深色主题
        Window w = getWindow();
        w.setStatusBarColor(Color.parseColor("#070a10"));
        w.setNavigationBarColor(Color.parseColor("#070a10"));

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#070a10"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#070a10"));
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // 曲库里点一下就能放
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setUserAgentString(s.getUserAgentString() + " MusicDeskApp/1.2.0");

        web.addJavascriptInterface(new Bridge(), "MDNative");
        web.setWebChromeClient(new WebChromeClient());
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                Uri u = req.getUrl();
                String scheme = u.getScheme() == null ? "" : u.getScheme();
                // http/https 留在壳里；其它（tel: mailto: 等）交出去
                if (scheme.startsWith("http")) return false;
                try {
                    startActivity(new android.content.Intent(android.content.Intent.ACTION_VIEW, u));
                } catch (Exception ignored) {
                }
                return true;
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame() && !isSetupPage(v.getUrl())) {
                    showOffline(v.getUrl());
                }
            }

            @Override
            public void onPageFinished(WebView v, String url) {
                // 引导页里注入当前已保存的地址 + 本机网段提示
                if (isSetupPage(url)) {
                    v.evaluateJavascript("window.MDInit && window.MDInit("
                            + jsStr(sp.getString(KEY_SERVER, "")) + ","
                            + jsStr(localIpPrefix()) + ","
                            + jsStr(sp.getString(KEY_MODE, "")) + ")", null);
                }
            }
        });

        String saved = sp.getString(KEY_SERVER, "");
        String mode = sp.getString(KEY_MODE, "");
        if ("local".equals(mode)) {
            bootLocal();
        } else if (saved == null || saved.trim().isEmpty()) {
            web.loadUrl(SETUP_URL);
        } else {
            web.loadUrl(saved);
        }
    }

    /** 起手机里的 Python 后端，然后把 WebView 指向它。 */
    private void bootLocal() {
        web.loadDataWithBaseURL(null, "<!doctype html><meta charset=utf-8>"
                + "<style>body{margin:0;height:100vh;display:flex;align-items:center;"
                + "justify-content:center;background:#070a10;color:#8b93a8;"
                + "font:15px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif}</style>"
                + "<div>正在启动本机引擎…</div>", "text/html", "utf-8", null);
        new Thread(new Runnable() {
            public void run() {
                final String url = startPython();
                runOnUiThread(new Runnable() {
                    public void run() {
                        if (url.isEmpty()) {
                            showLocalFailed();
                        } else {
                            web.loadUrl(url);
                        }
                    }
                });
            }
        }, "py-boot").start();
    }

    /** 第一次调用会加载整个 CPython 运行时，得几百毫秒到两秒；所以只能放在后台线程。 */
    private String startPython() {
        if (!localUrl.isEmpty()) return localUrl;
        try {
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(getApplicationContext()));
            }
            Python py = Python.getInstance();
            int port = py.getModule("mdphone")
                    .callAttr("start", getFilesDir().getAbsolutePath(), 8899)
                    .toInt();
            localUrl = "http://127.0.0.1:" + port + "/m";
            localError = "";
            return localUrl;
        } catch (Throwable e) {
            localError = e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage());
            return "";
        }
    }

    private void showLocalFailed() {
        String html = "<!doctype html><meta charset=utf-8>"
                + "<meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;height:100vh;display:flex;flex-direction:column;"
                + "align-items:center;justify-content:center;gap:16px;background:#070a10;"
                + "color:#e6e9f2;font:15px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
                + "padding:30px;text-align:center}"
                + "h2{font-size:18px;margin:0}p{margin:0;color:#8b93a8;font-size:13px;line-height:1.8}"
                + "code{background:#12161f;padding:8px 12px;border-radius:8px;color:#f87171;"
                + "font-size:11px;word-break:break-all;max-width:100%;display:block;text-align:left}"
                + "button{margin-top:8px;padding:13px 30px;border:0;border-radius:12px;font-size:15px;"
                + "font-weight:600;color:#fff;background:linear-gradient(135deg,#7c5cff,#22d3ee)}"
                + "</style><h2>本机引擎启动失败</h2>"
                + "<p>手机上的 Python 没能起来。</p>"
                + "<code>" + (localError.isEmpty() ? "未知错误" : localError) + "</code>"
                + "<button onclick='MDNative.reset()'>换成连接电脑</button>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private boolean isSetupPage(String url) {
        return url != null && url.startsWith("file:///android_asset/setup.html");
    }

    /** 连不上时给一个能重试、能改地址的页面，而不是浏览器那种死白页。 */
    private void showOffline(String failedUrl) {
        String html = "<!doctype html><meta charset=utf-8>"
                + "<meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;height:100vh;display:flex;flex-direction:column;"
                + "align-items:center;justify-content:center;gap:18px;background:#070a10;"
                + "color:#e6e9f2;font:16px/1.6 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
                + "padding:32px;text-align:center}"
                + "h2{font-size:19px;margin:0}p{margin:0;color:#8b93a8;font-size:14px;line-height:1.8}"
                + "code{background:#12161f;padding:2px 7px;border-radius:6px;color:#a78bfa;font-size:13px}"
                + "button{margin-top:6px;padding:13px 30px;border:0;border-radius:12px;font-size:16px;"
                + "font-weight:600;color:#fff;background:linear-gradient(135deg,#7c5cff,#22d3ee)}"
                + "a{color:#8b93a8;font-size:14px;text-decoration:underline}"
                + "</style><h2>连不上电脑</h2>"
                + "<p>确认这台手机和电脑在同一个 Wi-Fi，<br>电脑上的「音乐下载台」正在运行。</p>"
                + "<p>地址：<code>" + (failedUrl == null ? "" : failedUrl) + "</code></p>"
                + "<button onclick='location.reload()'>重试</button>"
                + "<a href='#' onclick='MDNative.reset();return false'>换一个地址</a>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    /** 电脑大概在哪个网段，引导页拿来做输入提示。 */
    private String localIpPrefix() {
        try {
            java.util.Enumeration<java.net.NetworkInterface> nics = java.net.NetworkInterface.getNetworkInterfaces();
            while (nics != null && nics.hasMoreElements()) {
                java.net.NetworkInterface nic = nics.nextElement();
                if (!nic.isUp()) continue;
                for (java.net.InterfaceAddress ia : nic.getInterfaceAddresses()) {
                    java.net.InetAddress a = ia.getAddress();
                    if (a == null || !(a instanceof java.net.Inet4Address) || a.isLoopbackAddress()) continue;
                    String ip = a.getHostAddress();
                    if (ip != null && ip.startsWith("192.168.")) {
                        return ip.substring(0, ip.lastIndexOf('.')) + ".";
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return "";
    }

    private static String jsStr(String s) {
        if (s == null) return "\"\"";
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"")
                .replace("\n", "\\n").replace("\r", "") + "\"";
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            String url = web.getUrl();
            if (isSetupPage(url)) {
                return super.onKeyDown(keyCode, event);
            }
            if (web.canGoBack()) {
                web.goBack();
                return true;
            }
            long now = System.currentTimeMillis();
            if (now - lastBackAt < 2000) {
                return super.onKeyDown(keyCode, event);
            }
            lastBackAt = now;
            Toast.makeText(this, "再按一次退出", Toast.LENGTH_SHORT).show();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    /** 只暴露启动/切换所需的最小能力，不给网页任意 Intent 的权限。 */
    private class Bridge {
        @JavascriptInterface
        public String getServer() {
            return sp.getString(KEY_SERVER, "");
        }

        @JavascriptInterface
        public void saveServer(String url) {
            sp.edit().putString(KEY_SERVER, url).apply();
        }

        @JavascriptInterface
        public String getMode() {
            return sp.getString(KEY_MODE, "");
        }

        /** 选「在这台手机上跑」：起本机引擎，成功再记下来。返回空串表示失败。 */
        @JavascriptInterface
        public String useLocal() {
            String url = startPython();
            if (!url.isEmpty()) {
                sp.edit().putString(KEY_MODE, "local").apply();
            }
            return url;
        }

        @JavascriptInterface
        public void useRemote(String url) {
            sp.edit().putString(KEY_MODE, "remote").putString(KEY_SERVER, url).apply();
        }

        @JavascriptInterface
        public void openApp() {
            final String mode = sp.getString(KEY_MODE, "");
            if ("local".equals(mode)) {
                bootLocal();
                return;
            }
            final String url = sp.getString(KEY_SERVER, "");
            if (url == null || url.isEmpty()) return;
            runOnUiThread(new Runnable() {
                public void run() {
                    web.loadUrl(url);
                }
            });
        }

        @JavascriptInterface
        public void reset() {
            sp.edit().remove(KEY_SERVER).remove(KEY_MODE).apply();
            runOnUiThread(new Runnable() {
                public void run() {
                    web.loadUrl(SETUP_URL);
                }
            });
        }

        @JavascriptInterface
        public String readClipboard() {
            try {
                ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
                ClipData d = cm.getPrimaryClip();
                if (d != null && d.getItemCount() > 0) {
                    CharSequence t = d.getItemAt(0).coerceToText(MainActivity.this);
                    return t == null ? "" : t.toString();
                }
            } catch (Exception ignored) {
            }
            return "";
        }

        @JavascriptInterface
        public void toast(String msg) {
            final String m = msg;
            runOnUiThread(new Runnable() {
                public void run() {
                    Toast.makeText(MainActivity.this, m, Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface
        public String version() {
            return "1.2.0";
        }
    }
}