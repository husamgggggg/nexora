Nexo Trade — بناء APK بدون دومين (Capacitor + WebView يفتح http://IP:8000)
================================================================================

0) آخر نسخة من المستودع
   من جذر المشروع nexora:
     git pull
   ثم افتح مجلد mobile-capacitor (أو mobile) وتابع الخطوات أدناه. لا يوجد ملف APK جاهز
   في Git — تبنيه محلياً بـ Android Studio. بعد Build → Build APK(s) يكون الملف عادة:
     mobile-capacitor/android/app/build/outputs/apk/debug/app-debug.apk
   (أو mobile/android/... بنفس المسار إذا استخدمت مجلد mobile)

1) عدّل عنوان السيرفر
   افتح: capacitor.config.json
   الحقل server.url الافتراضي http://127.0.0.1:8000 — غيّره لعنوان السيرفر العام، مثال:
     "url": "http://187.127.142.123:8000"
   على المحاكي (Android Emulator) لتجربة السيرفر على نفس جهاز الكمبيوتر:
     "url": "http://10.0.2.2:8000"
   على هاتف حقيقي على نفس الشبكة: استخدم IP اللابتوب على الـ LAN (مثلاً 192.168.x.x)

2) على السيرفر
   export ALLOWED_ORIGINS='*'
   شغّل bot.py على 0.0.0.0:8000 (الافتراضي في المشروع)

3) تثبيت الأدوات
   - Node.js 18+
   - Android Studio (SDK + Platform Tools)

4) أوامر البناء (من مجلد mobile-capacitor)
   npm install
   npx cap add android
   npx cap sync android
   npx cap open android

5) في Android Studio
   Build → Build Bundle(s) / APK(s) → Build APK(s)
   أو Run على جهاز/محاكي

ملاحظات أمنية
- HTTP على IP غير مشفّر؛ مناسب للتجربة.
- إن غيّر IP السيرفر حدّث capacitor.config.json ثم أعد Build.
