package com.ixxgal.musicdesk;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.media.MediaScannerConnection;
import android.net.ConnectivityManager;
import android.net.NetworkInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.Settings;
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
    private long pendingApkId = -1;
    private boolean blankChecked = false;
    private android.content.BroadcastReceiver apkReceiver;
    private long lastBackAt = 0;
    private volatile String localUrl = "";
    private volatile String localError = "";

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        sp = getSharedPreferences(PREF, MODE_PRIVATE);
        installCrashHandler();

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
        s.setUserAgentString(s.getUserAgentString() + " MusicDeskApp/1.2.7");

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
                checkBlank(v, url);
                // 引导页里注入当前已保存的地址 + 本机网段提示
                if (isSetupPage(url)) {
                    v.evaluateJavascript("window.MDInit && window.MDInit("
                            + jsStr(sp.getString(KEY_SERVER, "")) + ","
                            + jsStr(localIpPrefix()) + ","
                            + jsStr(sp.getString(KEY_MODE, "")) + ")", null);
                }
            }
        });

        apkReceiver = new android.content.BroadcastReceiver() {
            @Override
            public void onReceive(Context ctx, android.content.Intent it) {
                if (!android.app.DownloadManager.ACTION_DOWNLOAD_COMPLETE.equals(it.getAction())) return;
                long id = it.getLongExtra(android.app.DownloadManager.EXTRA_DOWNLOAD_ID, -1);
                if (id != pendingApkId) return;
                pendingApkId = -1;
                try {
                    android.app.DownloadManager dm =
                            (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                    Uri uri = dm.getUriForDownloadedFile(id);
                    if (uri == null) {
                        Toast.makeText(MainActivity.this, "下载失败，去通知栏看看", Toast.LENGTH_LONG).show();
                        return;
                    }
                    android.content.Intent inst = new android.content.Intent(android.content.Intent.ACTION_VIEW);
                    inst.setDataAndType(uri, "application/vnd.android.package-archive");
                    inst.addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION
                            | android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(inst);
                } catch (Throwable e) {
                    Toast.makeText(MainActivity.this, "装不了：" + e.getMessage(), Toast.LENGTH_LONG).show();
                }
            }
        };
        // Android 13+ 注册非系统专属广播必须显式指定导出标志，少了会抛 SecurityException。
        // 这里再包一层 try：装不了更新不该把整个界面拖死。
        try {
            android.content.IntentFilter f =
                    new android.content.IntentFilter(android.app.DownloadManager.ACTION_DOWNLOAD_COMPLETE);
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(apkReceiver, f, Context.RECEIVER_EXPORTED);
            } else {
                registerReceiver(apkReceiver, f);
            }
        } catch (Throwable ignore) {
        }

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

    /** 起手机里的 Python 后端，然后把 WebView 指向它。可以在任意线程调用。 */
    private void bootLocal() {
        runOnUiThread(new Runnable() {
            public void run() {
                web.loadDataWithBaseURL(null, "<!doctype html><meta charset=utf-8>"
                        + "<style>body{margin:0;height:100vh;display:flex;align-items:center;"
                        + "justify-content:center;background:#070a10;color:#8b93a8;"
                        + "font:15px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif}</style>"
                        + "<div>正在启动本机引擎…</div>", "text/html", "utf-8", null);
            }
        });
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

    /** 崩溃时把堆栈落到 files/crash.txt，下次启动能在错误页看到。 */
    private void installCrashHandler() {
        final Thread.UncaughtExceptionHandler prev = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
            public void uncaughtException(Thread th, Throwable e) {
                try {
                    java.io.File f = new java.io.File(getFilesDir(), "crash.txt");
                    java.io.FileWriter w = new java.io.FileWriter(f, false);
                    w.write(new java.util.Date().toString() + "\n");
                    w.write(android.util.Log.getStackTraceString(e));
                    w.close();
                } catch (Throwable ignore) {
                }
                if (prev != null) prev.uncaughtException(th, e);
            }
        });
    }

    /** 上一次崩溃留下的堆栈（有就返回，顺便删掉，避免反复弹）。 */
    private String takeCrashLog() {
        try {
            java.io.File f = new java.io.File(getFilesDir(), "crash.txt");
            if (!f.isFile()) return "";
            byte[] b = new byte[(int) f.length()];
            java.io.FileInputStream in = new java.io.FileInputStream(f);
            int n = in.read(b);
            in.close();
            f.delete();
            return n > 0 ? new String(b, 0, n, "UTF-8") : "";
        } catch (Throwable e) {
            return "";
        }
    }

    /** 有存储权限就用公共的「音乐/音乐下载台」，系统「音频」分类和文件管理器都能看见；没有就退回私有目录。 */
    private java.io.File dataRoot() {
        if (hasStorage()) {
            java.io.File d = new java.io.File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MUSIC),
                    "音乐下载台");
            if (d.isDirectory() || d.mkdirs()) return d;
        }
        return getFilesDir();
    }

    /** 是否拿到了写入公共目录的权限。 */
    private boolean hasStorage() {
        if (Build.VERSION.SDK_INT >= 30) {
            return Environment.isExternalStorageManager();
        }
        return checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
    }

    private String startPython() {
        if (!localUrl.isEmpty()) return localUrl;
        try {
            if (!Python.isStarted()) {
                Python.start(new AndroidPlatform(getApplicationContext()));
            }
            Python py = Python.getInstance();
            int port = py.getModule("mdphone")
                    .callAttr("start", dataRoot().getAbsolutePath(), 8899)
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
        // 上一次崩溃留下的堆栈也一起显示，省得没 adb 时只能干瞪眼
        String crash = takeCrashLog();
        String detail = localError.isEmpty() ? "未知错误" : localError;
        String dir = "";
        try {
            dir = dataRoot().getAbsolutePath();
        } catch (Throwable ignore) {
        }
        String html = "<!doctype html><meta charset=utf-8>"
                + "<meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;height:100vh;display:flex;flex-direction:column;"
                + "align-items:center;justify-content:center;gap:16px;background:#070a10;"
                + "color:#e6e9f2;font:15px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;"
                + "padding:30px;text-align:center}"
                + "h2{font-size:18px;margin:0}p{margin:0;color:#8b93a8;font-size:13px;line-height:1.8}"
                + "code{background:#12161f;padding:8px 12px;border-radius:8px;color:#f87171;"
                + "font-size:11px;word-break:break-all;max-width:100%;display:block;text-align:left;"
                + "max-height:220px;overflow:auto;white-space:pre-wrap}"
                + "button{margin-top:8px;padding:13px 30px;border:0;border-radius:12px;font-size:15px;"
                + "font-weight:600;color:#fff;background:linear-gradient(135deg,#7c5cff,#22d3ee)}"
                + "</style><h2>本机引擎启动失败</h2>"
                + "<p>手机上的 Python 没能起来。</p>"
                + "<code>" + detail + "</code>"
                + (dir.isEmpty() ? "" : "<p>数据目录：" + dir + "</p>")
                + (crash.isEmpty() ? "" : "<code>" + crash.replace("<", "&lt;") + "</code>")
                + "<button onclick='MDNative.reset()'>换成连接电脑</button>"
                + "<button onclick='location.reload()' style='background:#1b2130'>重试一次</button>";
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

    /** 本机引擎页面加载完仍是空的，就把「为什么空」摆给用户看，而不是留一片白。 */
    private void checkBlank(WebView v, String url) {
        if (url == null || !url.contains("127.0.0.1")) return;
        if (blankChecked) return;
        v.postDelayed(new Runnable() {
            public void run() {
                try {
                    v.evaluateJavascript(
                            "(function(){var b=document.body;"
                                    + "return JSON.stringify({txt:b?b.innerText.trim().length:-1,"
                                    + "html:b?b.innerHTML.length:-1,"
                                    + "title:document.title||''});})()",
                            new android.webkit.ValueCallback<String>() {
                                public void onReceiveValue(String value) {
                                    if (blankChecked) return;
                                    String raw = value == null ? "" : value;
                                    int txt = pickInt(raw, "txt");
                                    int htmlLen = pickInt(raw, "html");
                                    // 有字或者有结构就算正常，放过
                                    if (txt > 0 || htmlLen > 40) return;
                                    blankChecked = true;
                                    showBlankDiag(v.getUrl(), txt, htmlLen, raw);
                                }
                            });
                } catch (Throwable ignore) {
                }
            }
        }, 1400);
    }

    /** 从 JSON 串里抠一个整数字段，失败返回 -1。 */
    private int pickInt(String json, String key) {
        try {
            int i = json.indexOf("\"" + key + "\":");
            if (i < 0) return -1;
            int s = i + key.length() + 3;
            int e = s;
            while (e < json.length() && (json.charAt(e) == '-' || Character.isDigit(json.charAt(e)))) e++;
            if (e == s) return -1;
            return Integer.parseInt(json.substring(s, e));
        } catch (Throwable e) {
            return -1;
        }
    }

    /** 页面空白时的诊断页：告诉用户服务活没活、页面大小、以及能不能换连接方式。 */
    private void showBlankDiag(final String url, final int txt, final int htmlLen, final String raw) {
        // 探活要联网，不能在主线程干
        new Thread(new Runnable() {
            public void run() {
                String ping;
                try {
                    java.net.HttpURLConnection c = (java.net.HttpURLConnection)
                            new java.net.URL("http://127.0.0.1:8899/api/ping").openConnection();
                    c.setConnectTimeout(2500);
                    c.setReadTimeout(2500);
                    ping = "HTTP " + c.getResponseCode();
                    c.disconnect();
                } catch (Throwable e) {
                    ping = "异常：" + e.getClass().getSimpleName();
                }
                final String pingText = ping;
                runOnUiThread(new Runnable() {
                    public void run() {
                        renderBlankDiag(url, txt, htmlLen, raw, pingText);
                    }
                });
            }
        }).start();
    }

    private void renderBlankDiag(String url, int txt, int htmlLen, String raw, String ping) {
        String html = "<!doctype html><meta charset=utf-8>"
                + "<meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;min-height:100vh;display:flex;flex-direction:column;"
                + "align-items:center;justify-content:center;gap:14px;background:#070a10;"
                + "color:#e6e9f2;font:15px/1.7 -apple-system,'PingFang SC',sans-serif;padding:28px}"
                + "h2{font-size:18px;margin:0}p{margin:0;color:#8b93a8;font-size:13px;line-height:1.8;"
                + "text-align:center;word-break:break-all}"
                + "code{background:#12161f;padding:9px 12px;border-radius:8px;color:#f87171;"
                + "font-size:11px;word-break:break-all;display:block;max-width:100%;white-space:pre-wrap}"
                + "button{margin-top:6px;padding:13px 30px;border:0;border-radius:12px;font-size:15px;"
                + "font-weight:600;color:#fff;background:linear-gradient(135deg,#7c5cff,#22d3ee)}"
                + "</style><h2>界面没画出来</h2>"
                + "<p>本机服务：" + ping + "</p>"
                + "<p>页面地址：<br>" + (url == null ? "" : url) + "</p>"
                + "<code>文字长度 " + txt + " / HTML 长度 " + htmlLen + "\n" + raw.replace("<", "&lt;") + "</code>"
                + "<button onclick='location.reload()'>重新加载</button>"
                + "<button onclick='MDNative.reset()' style='background:#1b2130'>换成连接电脑</button>";
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
    protected void onDestroy() {
        try {
            if (apkReceiver != null) unregisterReceiver(apkReceiver);
        } catch (Throwable ignore) {
        }
        apkReceiver = null;
        super.onDestroy();
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 兜底：万一初始加载没发生，别让用户对着白屏
        try {
            if (web != null && web.getUrl() == null) {
                String m2 = sp.getString(KEY_MODE, "");
                if ("local".equals(m2)) bootLocal();
                else web.loadUrl(SETUP_URL);
            }
        } catch (Throwable ignore) {
        }
        // 回到前台扫一遍媒体库，新下的歌立刻出现在系统「音频」里
        try {
            java.io.File dir = new java.io.File(dataRoot(), "downloads");
            java.io.File[] fs = dir.listFiles();
            if (fs != null) {
                for (java.io.File f : fs) {
                    if (!f.isFile()) continue;
                    String n = f.getName().toLowerCase();
                    if (n.endsWith(".mp3") || n.endsWith(".m4a") || n.endsWith(".aac")
                            || n.endsWith(".flac") || n.endsWith(".ogg") || n.endsWith(".wav")) {
                        MediaScannerConnection.scanFile(this, new String[]{f.getAbsolutePath()}, null, null);
                    }
                }
            }
        } catch (Throwable ignore) {
        }
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
            // 已经在跑就直接用，别重复启动（第二次会卡在端口占用上几十秒）
            if (localUrl != null && !localUrl.isEmpty()) {
                sp.edit().putString(KEY_MODE, "local").apply();
                return localUrl;
            }
            String url = startPython();
            if (!url.isEmpty()) {
                sp.edit().putString(KEY_MODE, "local").apply();
            }
            return url;
        }

        /** 上一次本机引擎是否已经起过（前端据此显示「正在运行」而不是「启动」）。 */
        @JavascriptInterface
        public boolean localReady() {
            return localUrl != null && !localUrl.isEmpty();
        }

        @JavascriptInterface
        public void useRemote(String url) {
            sp.edit().putString(KEY_MODE, "remote").putString(KEY_SERVER, url).apply();
        }

        @JavascriptInterface
        public void openApp() {
            // 注意：这里跑在 WebView 的 JS 线程，碰 WebView 必须切回 UI 线程。
            runOnUiThread(new Runnable() {
                public void run() {
                    String mode = sp.getString(KEY_MODE, "");
                    if ("local".equals(mode)) {
                        bootLocal();
                        return;
                    }
                    String url = sp.getString(KEY_SERVER, "");
                    if (url == null || url.isEmpty()) return;
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
            return "1.2.7";
        }

        /** 用系统浏览器打开一个链接（更新下载页之类）。 */
        @JavascriptInterface
        public void openUrl(final String url) {
            if (url == null || url.isEmpty()) return;
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        android.content.Intent it = new android.content.Intent(
                                android.content.Intent.ACTION_VIEW, android.net.Uri.parse(url));
                        it.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                        startActivity(it);
                    } catch (Throwable e) {
                        Toast.makeText(MainActivity.this, "打不开链接", Toast.LENGTH_SHORT).show();
                    }
                }
            });
        }

        /** 下载目录的绝对路径，给设置面板显示用。 */
        @JavascriptInterface
        public String downloadDir() {
            return new java.io.File(dataRoot(), "downloads").getAbsolutePath();
        }

        /** 现在能不能写公共的「下载」目录。 */
        @JavascriptInterface
        public boolean storageOk() {
            return MainActivity.this.hasStorage();
        }

        /** 授权后会落到哪里（没权限时也把目标位置告诉用户）。 */
        @JavascriptInterface
        public String publicDir() {
            return new java.io.File(
                    Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MUSIC),
                    "音乐下载台/downloads").getAbsolutePath();
        }

        /** 申请「所有文件访问权限」（Android 11+）或普通存储权限（10 及以下）。 */
        @JavascriptInterface
        public void requestStorage() {
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        if (Build.VERSION.SDK_INT >= 30) {
                            android.content.Intent it = new android.content.Intent(
                                    Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                                    Uri.parse("package:" + getPackageName()));
                            startActivity(it);
                        } else {
                            requestPermissions(new String[]{
                                    android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, 1001);
                        }
                    } catch (Throwable e) {
                        try {
                            startActivity(new android.content.Intent(
                                    Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
                        } catch (Throwable e2) {
                            Toast.makeText(MainActivity.this,
                                    "找不到授权页面，去「设置 → 应用 → 音乐下载台 → 权限」里手动打开",
                                    Toast.LENGTH_LONG).show();
                        }
                    }
                }
            });
        }

        /** 把下载目录里的文件扫进系统媒体库，这样「音频/视频」分类里能直接看到。 */
        @JavascriptInterface
        public void scanDownloads() {
            final java.io.File dir = new java.io.File(dataRoot(), "downloads");
            new Thread(new Runnable() {
                public void run() {
                    java.io.File[] fs = dir.listFiles();
                    if (fs == null) return;
                    for (java.io.File f : fs) {
                        if (!f.isFile()) continue;
                        String n = f.getName().toLowerCase();
                        if (n.endsWith(".mp3") || n.endsWith(".m4a") || n.endsWith(".aac")
                                || n.endsWith(".flac") || n.endsWith(".ogg") || n.endsWith(".wav")
                                || n.endsWith(".mp4") || n.endsWith(".m4v") || n.endsWith(".mkv")) {
                            MediaScannerConnection.scanFile(MainActivity.this,
                                    new String[]{f.getAbsolutePath()}, null, null);
                        }
                    }
                }
            }, "md-scan").start();
        }

        /** 用系统下载器把新版本 APK 拉下来，下完直接弹安装。不用去浏览器。 */
        @JavascriptInterface
        public void downloadAndInstall(final String url, final String filename) {
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        // Android 8+ 需要「安装未知应用」权限
                        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
                            Toast.makeText(MainActivity.this,
                                    "先允许「安装未知应用」，回来再点一次更新", Toast.LENGTH_LONG).show();
                            try {
                                startActivity(new android.content.Intent(
                                        Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                        Uri.parse("package:" + getPackageName())));
                            } catch (Throwable ignore) {}
                            return;
                        }
                        android.app.DownloadManager dm =
                                (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                        String name = (filename == null || filename.isEmpty())
                                ? "MusicDesk-update.apk" : filename;
                        android.app.DownloadManager.Request req =
                                new android.app.DownloadManager.Request(Uri.parse(url));
                        req.setTitle("音乐下载台 新版本");
                        req.setDescription(name);
                        req.setMimeType("application/vnd.android.package-archive");
                        req.setNotificationVisibility(
                                android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                        req.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                        long id = dm.enqueue(req);
                        pendingApkId = id;
                        Toast.makeText(MainActivity.this, "开始下载，下完会自己弹安装", Toast.LENGTH_SHORT).show();
                    } catch (Throwable e) {
                        Toast.makeText(MainActivity.this, "下载失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
                    }
                }
            });
        }

        /** 打开系统「安装未知应用」的授权页。 */
        @JavascriptInterface
        public boolean canInstall() {
            return Build.VERSION.SDK_INT < 26 || getPackageManager().canRequestPackageInstalls();
        }

        @JavascriptInterface
        public void requestInstall() {
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        startActivity(new android.content.Intent(
                                Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                                Uri.parse("package:" + getPackageName())));
                    } catch (Throwable e) {
                        Toast.makeText(MainActivity.this, "打不开授权页，去设置里手动允许", Toast.LENGTH_LONG).show();
                    }
                }
            });
        }

        /** 授权回来后重启本机引擎，让新目录生效。 */
        @JavascriptInterface
        public void restartLocal() {
            runOnUiThread(new Runnable() {
                public void run() {
                    localUrl = "";
                    bootLocal();
                }
            });
        }

        /** 曲库里这个文件的完整路径（手机上就是应用私有目录）。 */
        @JavascriptInterface
        public String filePath(String name) {
            java.io.File f = new java.io.File(new java.io.File(dataRoot(), "downloads"), safeName(name));
            return f.getAbsolutePath();
        }

        /**
         * 用系统里的播放器打开这个文件。
         * 应用私有目录不能直接给外部 http/file URI（Android 7+ 会拦），
         * 所以先让媒体库扫一遍，拿到 content:// 再交给系统。
         */
        @JavascriptInterface
        public void openFile(final String name) {
            final java.io.File f = new java.io.File(new java.io.File(dataRoot(), "downloads"), safeName(name));
            if (!f.isFile()) {
                toast("文件不在了");
                return;
            }
            MediaScannerConnection.scanFile(
                    MainActivity.this,
                    new String[]{f.getAbsolutePath()},
                    null,
                    new MediaScannerConnection.OnScanCompletedListener() {
                        public void onScanCompleted(String path, android.net.Uri uri) {
                            if (uri == null) {
                                toast("系统没认出这个文件，路径：" + f.getAbsolutePath());
                                return;
                            }
                            String lower = name.toLowerCase();
                            String mime = lower.endsWith(".mp4") || lower.endsWith(".m4v")
                                    ? "video/*" : "audio/*";
                            android.content.Intent i = new android.content.Intent(android.content.Intent.ACTION_VIEW);
                            i.setDataAndType(uri, mime);
                            i.addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION
                                    | android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                            try {
                                startActivity(i);
                            } catch (Exception e) {
                                toast("这台手机上没找到能打开它的应用");
                            }
                        }
                    });
        }

        /** 只允许纯文件名，防 ../ 越界。 */
        private String safeName(String name) {
            if (name == null) return "";
            int i = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'));
            return i >= 0 ? name.substring(i + 1) : name;
        }
    }
}