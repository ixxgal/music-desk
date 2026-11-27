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

/**
 * 音乐下载台 · 手机端壳。
 *
 * 它本身不做任何下载 —— 后端跑在电脑上，这里只是一个全屏 WebView，
 * 记住电脑地址后直接打开那个网页，看起来和原生 App 一样。
 *
 * 地址来源：首次启动的引导页（assets/setup.html）里填，存 SharedPreferences。
 */
public class MainActivity extends Activity {

    private static final String PREF = "musicdesk";
    private static final String KEY_SERVER = "server";
    private static final String SETUP_URL = "file:///android_asset/setup.html";

    private WebView web;
    private SharedPreferences sp;
    private long lastBackAt = 0;

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
        s.setUserAgentString(s.getUserAgentString() + " MusicDeskApp/1.1.6");

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
                            + jsStr(sp.getString(KEY_SERVER, "")) + "," + jsStr(localIpPrefix()) + ")", null);
                }
            }
        });

        String saved = sp.getString(KEY_SERVER, "");
        if (saved == null || saved.trim().isEmpty()) {
            web.loadUrl(SETUP_URL);
        } else {
            web.loadUrl(saved);
        }
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
        public void openApp() {
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
            sp.edit().remove(KEY_SERVER).apply();
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
            return "1.1.6";
        }
    }
}