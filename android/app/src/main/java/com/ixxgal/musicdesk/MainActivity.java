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
import android.provider.MediaStore;
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
    private boolean blankRetried = false;
    private android.content.BroadcastReceiver apkReceiver;
    private long lastBackAt = 0;
    private volatile String localUrl = "";
    private volatile String localError = "";
    /** bootLocal 只允许跑一次。onResume 会在 onCreate 之后立刻来一次，没有这个锁就会
     *  起两个 py-boot 线程同时解压界面文件、同时抢端口，结果把 m.html 写成半截 ——
     *  页面空白但 /api/ping 正常，正是 1.2.7 白屏的真正原因。 */
    private final Object bootLock = new Object();
    private volatile boolean booting = false;
    /** 最近一次交给 WebView 的地址。WebView.getUrl() 在 loadDataWithBaseURL(null) 之后
     *  仍然是 null，不能拿它判断「有没有加载过」。 */
    private volatile String loadedUrl = "";

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
        // 页面是本机引擎现给的，缓存没有任何好处，却能把「某一刻的坏状态」永久固化下来
        // —— 用户那几次白屏就是这么来的：一旦缓存到半截 HTML，怎么 reload 都是它。
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setUserAgentString(s.getUserAgentString() + " MusicDeskApp/1.3.0");

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
                // 独立 App 里的页面是本地引擎提供的，Service Worker 只会添乱：
                // 1.2.9 之前它把 /m 也缓存了，一缓存到半截 HTML 就永远打不开。
                // 这里每次进本机页面都注销掉所有 SW + 清空 CacheStorage，一劳永逸。
                if (url != null && url.contains("127.0.0.1")) {
                    sweepServiceWorker(v);
                }
            }

            @Override
            public boolean onRenderProcessGone(WebView v, android.webkit.RenderProcessGoneDetail d) {
                // 渲染进程挂了：WebView 会变成一片白，evaluateJavascript 全部返回 null ——
                // 这正是用户那几张诊断页里「文字 -1 / HTML -1」的样子。
                boolean crashed = d != null && d.didCrash();
                android.util.Log.e("MusicDesk", "渲染进程没了 crashed=" + crashed);
                try {
                    java.io.File f = new java.io.File(getFilesDir(), "crash.txt");
                    java.io.FileWriter w = new java.io.FileWriter(f, false);
                    w.write(new java.util.Date().toString() + "\n");
                    w.write("WebView 渲染进程退出（didCrash=" + crashed + "）\n");
                    w.write(buildInfo());
                    w.close();
                } catch (Throwable ignore) {
                }
                // 返回 true 表示自己处理掉，别让系统把整个 App 杀掉
                showRenderGone(crashed);
                return true;
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
            // 安卓 9 及以下写公共目录要个存储权限，一进来就弹，别让用户自己去翻设置。
            // 安卓 10+ 用 MediaStore，压根不需要权限，这里不会打扰。
            if (Build.VERSION.SDK_INT < 29
                    && checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                    != android.content.pm.PackageManager.PERMISSION_GRANTED) {
                try {
                    requestPermissions(new String[]{
                            android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, 1001);
                } catch (Throwable ignore) {
                }
            }
            bootLocal();
        } else if (saved == null || saved.trim().isEmpty()) {
            loadedUrl = SETUP_URL;
            web.loadUrl(SETUP_URL);
        } else {
            loadedUrl = saved;
            web.loadUrl(saved);
        }
    }

    /** 起手机里的 Python 后端，然后把 WebView 指向它。可以在任意线程调用。 */
    private void bootLocal() {
        synchronized (bootLock) {
            if (booting) {
                android.util.Log.i("MusicDesk", "bootLocal 已在跑，忽略重复调用");
                return;
            }
            booting = true;
        }
        runOnUiThread(new Runnable() {
            public void run() {
                loadedUrl = "about:blank";
                web.loadDataWithBaseURL("about:blank", "<!doctype html><meta charset=utf-8>"
                        + "<style>body{margin:0;height:100vh;display:flex;align-items:center;"
                        + "justify-content:center;background:#070a10;color:#8b93a8;"
                        + "font:15px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif}</style>"
                        + "<div>正在启动本机引擎…</div>", "text/html", "utf-8", null);
            }
        });
        new Thread(new Runnable() {
            public void run() {
                try {
                    adoptOldDownloads();          // 老版本下在公共目录的，先搬回私有目录
                    final String url = startPython();
                    if (!url.isEmpty()) {
                        // 启动就把已有的歌补一份到系统「音乐」，用户不用点什么
                        publishAsync(false);
                    }
                    runOnUiThread(new Runnable() {
                        public void run() {
                            if (url.isEmpty()) {
                                loadedUrl = "about:blank";
                                showLocalFailed();
                            } else {
                                loadedUrl = url;
                                // 先把之前留下的 HTTP 缓存和 Service Worker 清掉再进页面 ——
                                // 用户那几次白屏就是缓存里躺着一份坏 HTML 造成的
                                web.clearCache(true);
                                sweepServiceWorker(web);
                                web.loadUrl(url);
                            }
                        }
                    });
                } finally {
                    booting = false;
                }
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

    /**
     * 下载和曲库的工作目录，永远用应用私有目录。
     *
     * 私有目录写文件不需要任何权限，所以下载永远能成功；下好之后再通过
     * publishAll() 用 MediaStore 复制一份到系统「音乐/音乐下载台」，那边安卓 10+
     * 完全不需要授权就能写。这样就不会出现「拿不到所有文件访问权限 → 一首也存不下」。
     */
    private java.io.File dataRoot() {
        return getFilesDir();
    }

    /** 系统「音频」里那份的落地目录（仅用于展示和 API 24~28 的直写）。 */
    private java.io.File musicDir() {
        return new java.io.File(
                Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_MUSIC),
                "音乐下载台");
    }

    /** 这份扩展名算音频还是视频。 */
    private static int mediaKind(String name) {
        String n = name.toLowerCase();
        if (n.endsWith(".mp4") || n.endsWith(".m4v") || n.endsWith(".mkv")
                || n.endsWith(".webm") || n.endsWith(".mov")) return 2;   // 视频
        if (n.endsWith(".mp3") || n.endsWith(".m4a") || n.endsWith(".aac")
                || n.endsWith(".flac") || n.endsWith(".ogg") || n.endsWith(".wav")
                || n.endsWith(".opus") || n.endsWith(".wma")) return 1;   // 音频
        return 0;
    }

    private static String mimeOf(String name) {
        String n = name.toLowerCase();
        if (n.endsWith(".mp3")) return "audio/mpeg";
        if (n.endsWith(".m4a") || n.endsWith(".aac")) return "audio/mp4";
        if (n.endsWith(".flac")) return "audio/flac";
        if (n.endsWith(".ogg") || n.endsWith(".opus")) return "audio/ogg";
        if (n.endsWith(".wav")) return "audio/wav";
        if (n.endsWith(".mp4") || n.endsWith(".m4v")) return "video/mp4";
        if (n.endsWith(".mkv")) return "video/x-matroska";
        if (n.endsWith(".webm")) return "video/webm";
        if (n.endsWith(".mov")) return "video/quicktime";
        return "application/octet-stream";
    }

    /** 是否拿到了写公共目录的权限（只有 API 24~28 才需要）。 */
    private boolean hasStorage() {
        if (Build.VERSION.SDK_INT >= 30) {
            return Environment.isExternalStorageManager();
        }
        return checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE)
                == android.content.pm.PackageManager.PERMISSION_GRANTED;
    }

    /** 这个文件是不是已经发到系统音乐库了（按 名字+大小 记，避免重复插一份）。 */
    private boolean published(String key) {
        return sp.getBoolean("pub_" + key, false);
    }

    private void markPublished(String key) {
        sp.edit().putBoolean("pub_" + key, true).apply();
    }

    /**
     * 把私有 downloads 里的文件放一份到系统「音乐/音乐下载台」。
     *
     * 安卓 10+ 先走 MediaStore（不需要任何权限）；万一被 ROM 拒了就退回直接写公共目录
     * （有 MANAGE_EXTERNAL_STORAGE 时可行）。两条都不通才算失败。
     * 只读不删：私有那份继续给曲库播放和删除用。
     */
    private int publishOne(java.io.File f) {
        if (!f.isFile()) return 0;
        String name = f.getName();
        int kind = mediaKind(name);
        if (kind == 0) return 0;
        String key = name.replaceAll("[^0-9A-Za-z\\u4e00-\\u9fa5._-]", "_")
                + "@" + f.length();
        if (published(key)) return 0;

        boolean done = false;
        if (Build.VERSION.SDK_INT >= 29) {
            try {
                done = publishToMediaStore(f, name, kind);
            } catch (Throwable e) {
                android.util.Log.w("MusicDesk", "MediaStore 失败，改试直写: " + name, e);
            }
        }
        if (!done) {
            try {
                done = publishToPublicDir(f, name);
            } catch (Throwable e) {
                android.util.Log.w("MusicDesk", "直写公共目录失败: " + name, e);
            }
        }
        if (done) {
            markPublished(key);
            return 1;
        }
        return 0;
    }

    /** 安卓 10+ 的正路：MediaStore 插一条记录再把内容写进去，全程不需要权限。 */
    private boolean publishToMediaStore(java.io.File f, String name, int kind)
            throws Exception {
        android.content.ContentResolver cr = getContentResolver();
        boolean video = (kind == 2);
        android.net.Uri coll = video
                ? MediaStore.Video.Media.EXTERNAL_CONTENT_URI
                : MediaStore.Audio.Media.EXTERNAL_CONTENT_URI;
        String rel = video ? "Movies/音乐下载台" : "Music/音乐下载台";

        // 已经有同名同大小的就不重复插一份
        String sel = MediaStore.MediaColumns.DISPLAY_NAME + "=? AND "
                + MediaStore.MediaColumns.SIZE + "=?";
        try (android.database.Cursor c = cr.query(coll,
                new String[]{MediaStore.MediaColumns._ID}, sel,
                new String[]{name, String.valueOf(f.length())}, null)) {
            if (c != null && c.moveToFirst()) return true;
        }

        android.content.ContentValues v = new android.content.ContentValues();
        v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
        v.put(MediaStore.MediaColumns.MIME_TYPE, mimeOf(name));
        v.put(MediaStore.MediaColumns.RELATIVE_PATH, rel);
        v.put(MediaStore.MediaColumns.IS_PENDING, 1);
        android.net.Uri uri = cr.insert(coll, v);
        if (uri == null) return false;

        java.io.OutputStream os = cr.openOutputStream(uri);
        if (os == null) {
            cr.delete(uri, null, null);
            return false;
        }
        long total = 0;
        try {
            java.io.InputStream in = new java.io.FileInputStream(f);
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) {
                os.write(buf, 0, n);
                total += n;
            }
            in.close();
            os.flush();
        } finally {
            try {
                os.close();
            } catch (Throwable ignore) {
            }
        }
        if (total <= 0) {
            cr.delete(uri, null, null);
            return false;
        }
        android.content.ContentValues fix = new android.content.ContentValues();
        fix.put(MediaStore.MediaColumns.IS_PENDING, 0);
        cr.update(uri, fix, null, null);
        return true;
    }

    /** 兜底：直接往公共目录写一份（安卓 9 及以下的正路，10+ 只有授权了才走得通）。 */
    private boolean publishToPublicDir(java.io.File f, String name) throws Exception {
        if (!hasStorage()) return false;
        java.io.File dir = new java.io.File(musicDir(), "downloads");
        if (!dir.isDirectory() && !dir.mkdirs()) return false;
        java.io.File dst = new java.io.File(dir, name);
        if (dst.isFile() && dst.length() == f.length()) {
            MediaScannerConnection.scanFile(this, new String[]{dst.getAbsolutePath()}, null, null);
            return true;
        }
        java.io.FileOutputStream out = new java.io.FileOutputStream(dst);
        java.io.InputStream in = new java.io.FileInputStream(f);
        byte[] buf = new byte[65536];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        in.close();
        out.close();
        MediaScannerConnection.scanFile(this, new String[]{dst.getAbsolutePath()}, null, null);
        return true;
    }

    /** 一次把还没发过的都发出去，返回新存了几个。前端刷新曲库、任务完成时各调一次。 */
    private int publishAllNow() {
        java.io.File dir = new java.io.File(dataRoot(), "downloads");
        java.io.File[] fs = dir.listFiles();
        if (fs == null) return 0;
        int n = 0;
        for (java.io.File f : fs) {
            n += publishOne(f);
        }
        return n;
    }

    /**
     * 老版本（1.2.5 及以前）在有权限时会把歌直接下到公共目录，
     * 现在统一改到私有目录，先把旧的搬回来，免得升级后曲库变空。
     */
    private void adoptOldDownloads() {
        try {
            java.io.File old = new java.io.File(musicDir(), "downloads");
            if (!old.isDirectory()) return;
            java.io.File[] fs = old.listFiles();
            if (fs == null) return;
            java.io.File dstDir = new java.io.File(dataRoot(), "downloads");
            if (!dstDir.isDirectory() && !dstDir.mkdirs()) return;
            for (java.io.File f : fs) {
                if (!f.isFile()) continue;
                java.io.File dst = new java.io.File(dstDir, f.getName());
                if (dst.isFile() && dst.length() == f.length()) continue;
                java.io.FileInputStream in = new java.io.FileInputStream(f);
                java.io.FileOutputStream out = new java.io.FileOutputStream(dst);
                byte[] buf = new byte[65536];
                int n;
                while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                in.close();
                out.close();
            }
        } catch (Throwable ignore) {
        }
    }

    /** 后台发一份到系统音乐库。toast=true 时成功了会弹一句提示。 */
    private void publishAsync(final boolean toast) {
        new Thread(new Runnable() {
            public void run() {
                int n = 0;
                try {
                    n = publishAllNow();
                } catch (Throwable e) {
                    android.util.Log.w("MusicDesk", "publish failed", e);
                }
                if (n > 0 && toast) {
                    final int cnt = n;
                    runOnUiThread(new Runnable() {
                        public void run() {
                            Toast.makeText(MainActivity.this,
                                    "已存 " + cnt + " 首到「音乐/音乐下载台」，"
                                            + "打开文件管理器的「音频」就能看到",
                                    Toast.LENGTH_LONG).show();
                        }
                    });
                }
            }
        }, "md-publish").start();
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
            // 带一个每次都变的参数：万一手机上还留着旧 Service Worker（它在 1.2.9 之前
            // 把 /m 也缓存了，一缓存到半截 HTML 就再也进不来），带 ?t= 的导航一定绕过缓存。
            localUrl = "http://127.0.0.1:" + port + "/m?t=" + System.currentTimeMillis();
            localError = "";
            return localUrl;
        } catch (Throwable e) {
            localError = e.getClass().getSimpleName() + ": " + String.valueOf(e.getMessage());
            return "";
        }
    }

    /** 设备 + WebView 版本，写进 crash.txt 方便定位。 */
    private String buildInfo() {
        String wv = "";
        try {
            if (android.os.Build.VERSION.SDK_INT >= 26) {
                android.content.pm.PackageInfo pi =
                        android.webkit.WebView.getCurrentWebViewPackage();
                if (pi != null) wv = " webview=" + pi.packageName + "/" + pi.versionName;
            }
        } catch (Throwable ignore) {
        }
        return "app=1.3.0 sdk=" + android.os.Build.VERSION.SDK_INT
                + " device=" + android.os.Build.MANUFACTURER + " " + android.os.Build.MODEL
                + wv + "\n";
    }

    /** 渲染进程退出后的兜底页：告诉用户点一下就能重开，而不是干瞪一片白。 */
    private void showRenderGone(final boolean crashed) {
        runOnUiThread(new Runnable() {
            public void run() {
                String html = "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                        + "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                        + "<style>body{margin:0;min-height:100vh;display:flex;align-items:center;"
                        + "justify-content:center;background:#070a10;color:#e6ebf5;"
                        + "font:15px/1.75 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif}"
                        + ".w{padding:28px;max-width:420px;text-align:center}"
                        + "h1{font-size:19px;margin:0 0 10px}"
                        + "p{color:#8b93a8;font-size:13.5px;margin:0 0 8px}"
                        + "code{color:#8b93a8;font-size:11.5px;display:block;margin:12px 0;"
                        + "word-break:break-all;opacity:.7}"
                        + "button{margin-top:18px;width:100%;height:46px;border:0;border-radius:12px;"
                        + "background:linear-gradient(135deg,#6b5bff,#38bdf8);color:#fff;font-size:15.5px}"
                        + "</style></head><body><div class=\"w\">"
                        + "<h1>界面渲染进程退出了</h1>"
                        + "<p>安卓的 WebView 渲染进程被系统回收了"
                        + (crashed ? "（它自己崩了）" : "（内存不足被清理）") + "，所以界面是一片白。</p>"
                        + "<p>歌、设置、登录态都没丢，点下面重开一次就好。</p>"
                        + "<code>" + android.os.Build.MANUFACTURER + " " + android.os.Build.MODEL
                        + " · sdk " + android.os.Build.VERSION.SDK_INT + "</code>"
                        + "<button onclick=\"try{MDNative.reopen()}catch(e){location.reload()}\">"
                        + "重开界面</button>"
                        + "</div></body></html>";
                try {
                    web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
                } catch (Throwable ignore) {
                }
            }
        });
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

    /**
     * 注销所有 Service Worker + 清空 CacheStorage。
     *
     * 独立模式下的页面是本地引擎给的，离线缓存没有任何好处；而 1.2.9 之前的 sw.js
     * 把 '/m' 也放进 SHELL，只要某次缓存到半截 HTML，之后每次打开都拿到那份坏页面
     * （document.body 直接是 null），连 reload 都救不回来。这里每次进本机页面清一遍。
     */
    private void sweepServiceWorker(WebView v) {
        try {
            v.evaluateJavascript(
                    "(function(){try{"
                            + "if(navigator.serviceWorker&&navigator.serviceWorker.getRegistrations){"
                            + "navigator.serviceWorker.getRegistrations().then(function(rs){"
                            + "rs.forEach(function(r){r.unregister()})})}"
                            + "if(window.caches&&caches.keys){caches.keys().then(function(ks){"
                            + "ks.forEach(function(k){caches.delete(k)})})}"
                            + "}catch(e){}})()", null);
        } catch (Throwable ignore) {
        }
    }

    /** 本机引擎页面加载完仍是空的，就把「为什么空」摆给用户看，而不是留一片白。 */
    private void checkBlank(WebView v, String url) {
        if (url == null || !url.contains("127.0.0.1")) return;
        if (blankChecked) return;
        final String target = url;
        v.postDelayed(new Runnable() {
            public void run() {
                probeOnce(target, 0);
            }
        }, 1600);
    }

    /**
     * 探一次页面里到底有没有东西。
     *
     * 有个坑：`evaluateJavascript` 在文档还没 commit、或者调用时正在导航，会直接回调
     * `null` —— 那不是「页面是空的」，是「这次没问到」。上一版把 null 当成空，
     * 于是刚进 App 就弹诊断页，反而掩盖了真正能加载出来的页面。所以这里分开处理：
     * 问到 null 就隔一会儿再问，连着问不到才认定 JS 不可用。
     */
    private void probeOnce(final String target, final int attempt) {
        if (blankChecked) return;
        try {
            web.evaluateJavascript(
                    "(function(){var b=document.body;"
                            + "return JSON.stringify({txt:b?b.innerText.trim().length:-1,"
                            + "html:b?b.innerHTML.length:-1,"
                            + "title:document.title||''});})()",
                    new android.webkit.ValueCallback<String>() {
                        public void onReceiveValue(String value) {
                            if (blankChecked) return;
                            if (value == null || "null".equals(value.trim())) {
                                // 这一次没问到（文档还没 commit / 导航被打断），再等等
                                if (attempt < 6) {
                                    web.postDelayed(new Runnable() {
                                        public void run() {
                                            probeOnce(target, attempt + 1);
                                        }
                                    }, 700);
                                } else if (!blankRetried) {
                                    // 连问 6 次都问不到 —— 文档压根没建起来。
                                    // 清干净、换个地址重来一次，再不行才认输。
                                    blankRetried = true;
                                    android.util.Log.w("MusicDesk", "连问 6 次都拿不到页面状态，重载: " + target);
                                    sweepServiceWorker(web);
                                    web.clearCache(true);
                                    String bust = target + (target.contains("?") ? "&" : "?")
                                            + "r=" + System.currentTimeMillis();
                                    web.loadUrl(bust);
                                    checkBlank(web, bust);
                                } else {
                                    blankChecked = true;
                                    android.util.Log.w("MusicDesk", "重载后依然拿不到页面状态: " + target);
                                    showBlankDiag(target, -1, -1, "evaluateJavascript 一直是 null");
                                }
                                return;
                            }
                            String raw = value;
                            int txt = pickInt(raw, "txt");
                            int htmlLen = pickInt(raw, "html");
                            // 有字或者有结构就算正常，放过
                            if (txt > 0 || htmlLen > 40) return;
                            // 第一次空：多半是加载被打断、或者命中了旧的缓存，
                            // 先清缓存再用带新参数的地址重来一次
                            if (!blankRetried) {
                                blankRetried = true;
                                android.util.Log.w("MusicDesk", "页面为空，清缓存后重载: " + target);
                                sweepServiceWorker(web);
                                web.clearCache(true);
                                String bust = target + (target.contains("?") ? "&" : "?")
                                        + "r=" + System.currentTimeMillis();
                                web.loadUrl(bust);
                                checkBlank(web, bust);
                                return;
                            }
                            blankChecked = true;
                            showBlankDiag(target, txt, htmlLen, raw);
                        }
                    });
        } catch (Throwable ignore) {
        }
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
                // 端口用页面自己那个，不写死 8899 —— 引擎换过端口时写死就查错地方了
                String base = "http://127.0.0.1:8899";
                try {
                    java.net.URL u = new java.net.URL(String.valueOf(url));
                    int p = u.getPort();
                    base = "http://127.0.0.1:" + (p > 0 ? p : 80);
                } catch (Throwable ignore) {
                }
                String ping;
                try {
                    java.net.HttpURLConnection c = (java.net.HttpURLConnection)
                            new java.net.URL(base + "/api/ping").openConnection();
                    c.setConnectTimeout(2500);
                    c.setReadTimeout(2500);
                    ping = "HTTP " + c.getResponseCode();
                    c.disconnect();
                } catch (Throwable e) {
                    ping = "异常：" + e.getClass().getSimpleName();
                }
                final String pingText = ping;
                final String headText = fetchHead(base + "/m", 700);
                runOnUiThread(new Runnable() {
                    public void run() {
                        renderBlankDiag(url, txt, htmlLen, raw, pingText, headText);
                    }
                });
            }
        }).start();
    }

    /** 取服务端 /m 的前几百个字符 —— 一眼能看出是 404、空串，还是被写坏的半截 HTML。 */
    private String fetchHead(String u, int chars) {
        try {
            java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL(u).openConnection();
            c.setConnectTimeout(2500);
            c.setReadTimeout(2500);
            int code = c.getResponseCode();
            java.io.InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            if (in == null) {
                c.disconnect();
                return "HTTP " + code + " / 没有响应体";
            }
            byte[] b = new byte[chars * 3];
            int got = 0, r;
            while (got < b.length && (r = in.read(b, got, b.length - got)) > 0) got += r;
            in.close();
            c.disconnect();
            String s = new String(b, 0, got, "UTF-8");
            if (s.length() > chars) s = s.substring(0, chars) + "…";
            return "HTTP " + code + " / " + got + " bytes\n" + s;
        } catch (Throwable e) {
            return "请求失败：" + e.getClass().getSimpleName() + " " + e.getMessage();
        }
    }

    private void renderBlankDiag(String url, int txt, int htmlLen, String raw, String ping, String head) {
        String html = "<!doctype html><meta charset=utf-8>"
                + "<meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<style>body{margin:0;min-height:100vh;display:flex;flex-direction:column;"
                + "align-items:center;justify-content:center;gap:14px;background:#070a10;"
                + "color:#e6e9f2;font:15px/1.7 -apple-system,'PingFang SC',sans-serif;padding:24px}"
                + "h2{font-size:18px;margin:0}p{margin:0;color:#8b93a8;font-size:13px;line-height:1.8;"
                + "text-align:center;word-break:break-all}"
                + "code{background:#12161f;padding:9px 12px;border-radius:8px;color:#f87171;"
                + "font-size:11px;word-break:break-all;display:block;max-width:100%;"
                + "white-space:pre-wrap;max-height:300px;overflow:auto;text-align:left}"
                + "button{margin-top:6px;padding:13px 30px;border:0;border-radius:12px;font-size:15px;"
                + "font-weight:600;color:#fff;background:linear-gradient(135deg,#7c5cff,#22d3ee)}"
                + "</style><h2>界面没画出来</h2>"
                + "<p>本机服务：<b>" + ping + "</b></p>"
                + "<p>页面地址：<br>" + (url == null ? "" : url) + "</p>"
                + "<p>页面里：文字 " + txt + " 字 / HTML " + htmlLen + " 字</p>"
                + "<p style='font-size:12px;color:#6b7488'>服务器返回的 /m 开头：</p>"
                + "<code>" + head.replace("<", "&lt;") + "</code>"
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
        // 兜底：万一初始加载真没发生，别让用户对着白屏。
        // 注意不能用 web.getUrl() 判断 —— loadDataWithBaseURL 之后它仍是 null，
        // 而 onResume 紧跟在 onCreate 后面就会来一次，会造成 bootLocal 重入（1.2.7 白屏的根因）。
        // 所以延后 2 秒、且只认 loadedUrl 这个我们自己的标志。
        try {
            if (web != null) {
                web.postDelayed(new Runnable() {
                    public void run() {
                        try {
                            if (loadedUrl.isEmpty() && !booting) {
                                android.util.Log.w("MusicDesk", "加载标志为空，重来一次");
                                String m2 = sp.getString(KEY_MODE, "");
                                if ("local".equals(m2)) bootLocal();
                                else {
                                    loadedUrl = SETUP_URL;
                                    web.loadUrl(SETUP_URL);
                                }
                            }
                        } catch (Throwable ignore) {
                        }
                    }
                }, 2000);
            }
        } catch (Throwable ignore) {
        }
        // 回到前台补发一次：在别处下的、或者上次没发成功的，这次一起送去系统「音乐」
        try {
            new Thread(new Runnable() {
                public void run() {
                    try {
                        int n = publishAllNow();
                        if (n > 0) android.util.Log.i("MusicDesk", "publishAll on resume: " + n);
                    } catch (Throwable ignore) {
                    }
                }
            }, "md-publish-resume").start();
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
                    loadedUrl = url;
                    web.loadUrl(url);
                }
            });
        }

        @JavascriptInterface
        public void reset() {
            sp.edit().remove(KEY_SERVER).remove(KEY_MODE).apply();
            runOnUiThread(new Runnable() {
                public void run() {
                    loadedUrl = SETUP_URL;
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
            return "1.3.0";
        }

        /** 把整个界面重建一遍（渲染进程崩了之后的「重开界面」用）。 */
        @JavascriptInterface
        public void reopen() {
            runOnUiThread(new Runnable() {
                public void run() {
                    recreate();
                }
            });
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

        /** 现在是不是「不用授权也能存到系统音乐库」。安卓 10+ 走 MediaStore，恒为 true。 */
        @JavascriptInterface
        public boolean storageOk() {
            if (Build.VERSION.SDK_INT >= 29) return true;
            return MainActivity.this.hasStorage();
        }

        /** 歌最终会出现在系统的哪个文件夹（展示用，权限无关）。 */
        @JavascriptInterface
        public String publicDir() {
            return "音乐/音乐下载台";
        }

        /** 安卓 9 及以下要普通存储权限；10 及以上用 MediaStore，不需要任何权限，这里直接不动。 */
        @JavascriptInterface
        public void requestStorage() {
            if (Build.VERSION.SDK_INT >= 29) return;
            runOnUiThread(new Runnable() {
                public void run() {
                    try {
                        requestPermissions(new String[]{
                                android.Manifest.permission.WRITE_EXTERNAL_STORAGE}, 1001);
                    } catch (Throwable e) {
                        Toast.makeText(MainActivity.this,
                                "去「设置 → 应用 → 音乐下载台 → 权限」里打开存储权限",
                                Toast.LENGTH_LONG).show();
                    }
                }
            });
        }

        /**
         * 把私有目录里还没发出去的文件放一份到系统「音乐/音乐下载台」。
         * 后台线程做，复制大文件不会卡界面；有新存进去的会弹一句提示。
         */
        @JavascriptInterface
        public void publishAll() {
            publishAsync(true);
        }

        /** 静默版本：不弹提示（刷新曲库时用）。 */
        @JavascriptInterface
        public void publishQuiet() {
            publishAsync(false);
        }

        /** 兼容旧调用：现在等于 publishAll()，把下载目录里的文件补送到系统「音乐」。 */
        @JavascriptInterface
        public void scanDownloads() {
            new Thread(new Runnable() {
                public void run() {
                    try {
                        publishAllNow();
                    } catch (Throwable ignore) {
                    }
                }
            }, "md-publish").start();
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