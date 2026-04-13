# تطبيق Android (APK) — Nexo Trade

التطبيق **غلاف WebView** عبر [Capacitor](https://capacitorjs.com/) يفتح موقعك الحي من `server.url` في `capacitor.config.json` (افتراضيًا `https://aboodtrading.com`). غيّر الرابط إذا كان الدومين مختلفًا.

## المتطلبات

- [Node.js](https://nodejs.org/) (LTS)
- [Android Studio](https://developer.android.com/studio) مع Android SDK و **JDK 17**

## البناء

```bash
cd mobile
npm install
npx cap sync android
npx cap open android
```

في Android Studio:

1. **Build → Build Bundle(s) / APK(s) → Build APK(s)**
2. ملف **Debug APK** يظهر عادة تحت:  
   `mobile/android/app/build/outputs/apk/debug/app-debug.apk`

للنشر على Google Play تحتاج **توقيع Release** (keystore) عبر **Build → Generate Signed Bundle / APK**.

## بعد تغيير الدومين

1. عدّل `capacitor.config.json` → `server.url`
2. تأكد أن `ALLOWED_ORIGINS` في خادم FastAPI يتضمن أصل الموقع (مثل `https://aboodtrading.com`)
3. نفّذ `npx cap sync android` ثم أعد بناء الـ APK

## ملاحظات

- الأيقونة الافتراضية هي أيقونة Capacitor؛ استبدلها من `android/app/src/main/res/mipmap-*`
- المجلد `node_modules` لا يُرفع عادةً؛ شغّل `npm install` على كل جهاز جديد
