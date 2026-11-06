package tv.lyrics.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.KeyEvent
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.WindowManager
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout

/**
 * Полноэкранный WebView с экраном текста. Всё готовит сервер, здесь только показ.
 *
 * onStop → about:blank: SSE рвётся, и сервер перестаёт опрашивать Spotify, пока ТВ выключен.
 */
class MainActivity : Activity() {

    private lateinit var web: WebView
    private val handler = Handler(Looper.getMainLooper())
    private var dialog: AlertDialog? = null
    private var started = false

    private val retry = Runnable { if (started) web.loadUrl(screenUrl()) }
    private val openSettings = Runnable { showUrlDialog() }

    private val prefs by lazy { getSharedPreferences("lyrics", MODE_PRIVATE) }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.decorView.setBackgroundColor(Color.BLACK)

        if (BuildConfig.WEBVIEW_DEBUG) WebView.setWebContentsDebuggingEnabled(true)

        web = WebView(this).apply {
            setBackgroundColor(Color.BLACK)
            isFocusable = false
            isFocusableInTouchMode = false
            isVerticalScrollBarEnabled = false
            isHorizontalScrollBarEnabled = false
            setLayerType(View.LAYER_TYPE_HARDWARE, null)
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                mediaPlaybackRequiresUserGesture = true
                cacheMode = WebSettings.LOAD_DEFAULT
                setSupportZoom(false)
                builtInZoomControls = false
                displayZoomControls = false
                loadWithOverviewMode = false
                useWideViewPort = false
            }
            webViewClient = Client()
        }
        setContentView(web, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT))
        hideSystemUi()

        if (!isUsable(screenUrl())) handler.post { showUrlDialog() }
    }

    override fun onStart() {
        super.onStart()
        started = true
        hideSystemUi()
        val url = screenUrl()
        if (isUsable(url)) web.loadUrl(url)
    }

    override fun onStop() {
        started = false
        handler.removeCallbacks(retry)
        handler.removeCallbacks(openSettings)
        web.stopLoading()
        web.loadUrl("about:blank")
        super.onStop()
    }

    override fun onDestroy() {
        dialog?.dismiss()
        web.destroy()
        super.onDestroy()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUi()
    }

    // ── пульт: «Меню» или долгое OK (1.5 с) открывают смену адреса ──

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        when (event.keyCode) {
            KeyEvent.KEYCODE_MENU -> {
                if (event.action == KeyEvent.ACTION_UP) showUrlDialog()
                return true
            }
            KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER -> {
                if (dialog?.isShowing == true) return super.dispatchKeyEvent(event)
                if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) {
                    handler.postDelayed(openSettings, LONG_PRESS_MS)
                } else if (event.action == KeyEvent.ACTION_UP) {
                    handler.removeCallbacks(openSettings)
                }
                return true
            }
        }
        return super.dispatchKeyEvent(event)
    }

    private fun showUrlDialog() {
        if (dialog?.isShowing == true) return
        val input = EditText(this).apply {
            setText(screenUrl())
            setSelectAllOnFocus(false)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            isSingleLine = true
        }
        val pad = (24 * resources.displayMetrics.density).toInt()
        val box = FrameLayout(this).apply {
            setPadding(pad, pad / 2, pad, 0)
            addView(input)
        }
        dialog = AlertDialog.Builder(this)
            .setTitle(R.string.url_title)
            .setMessage(R.string.url_hint)
            .setView(box)
            .setPositiveButton(R.string.save) { _, _ ->
                val url = input.text.toString().trim()
                prefs.edit().putString(KEY_URL, url).apply()
                reload()
            }
            .setNeutralButton(R.string.reset) { _, _ ->
                prefs.edit().remove(KEY_URL).apply()
                reload()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .setOnDismissListener { hideSystemUi() }
            .create()
        dialog?.show()
        input.requestFocus()
        input.setSelection(input.text.length)
    }

    private fun reload() {
        handler.removeCallbacks(retry)
        val url = screenUrl()
        if (isUsable(url)) web.loadUrl(url) else showUrlDialog()
    }

    private fun screenUrl(): String = prefs.getString(KEY_URL, null)?.takeIf { it.isNotBlank() }
        ?: BuildConfig.LYRICS_URL

    private fun isUsable(url: String): Boolean {
        if (url.isBlank() || url.contains("CHANGE_ME")) return false
        val scheme = Uri.parse(url).scheme
        return scheme == "http" || scheme == "https"
    }

    @Suppress("DEPRECATION")
    private fun hideSystemUi() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            window.setDecorFitsSystemWindows(false)
            window.insetsController?.let {
                it.hide(WindowInsets.Type.systemBars())
                it.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_STABLE)
        }
    }

    private fun showOffline() {
        web.loadUrl(OFFLINE_URL)
        handler.removeCallbacks(retry)
        if (started) handler.postDelayed(retry, RETRY_MS)
    }

    private inner class Client : WebViewClient() {
        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame && request.url.toString() != OFFLINE_URL) showOffline()
        }

        override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
            // 403 — это осмысленная страница сервера («неверный ключ»), а 5xx — сервер лежит
            if (request.isForMainFrame && response.statusCode >= 500) showOffline()
        }

        @Deprecated("Нужен для API 23")
        override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean = false
    }

    companion object {
        private const val KEY_URL = "url"
        private const val OFFLINE_URL = "file:///android_asset/offline.html"
        private const val RETRY_MS = 5_000L
        private const val LONG_PRESS_MS = 1_500L
    }
}
