package com.nexora.trade

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity

/**
 * يفتح واجهة NEXORA من الخادم عبر IP:8000 — بدون دومين.
 * غيّر العنوان في res/values/strings.xml → nexora_server_url
 */
class MainActivity : AppCompatActivity() {
    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val wv = WebView(this)
        setContentView(wv)
        wv.settings.javaScriptEnabled = true
        wv.settings.domStorageEnabled = true
        wv.webViewClient = WebViewClient()
        wv.loadUrl(getString(R.string.nexora_server_url))
    }
}
