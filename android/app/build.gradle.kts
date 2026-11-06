plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

fun prop(name: String, default: String) = (project.findProperty(name) as String?) ?: default

android {
    namespace = "tv.lyrics.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "tv.lyrics.app"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
        val url = prop("lyrics.url", "").replace("\\", "\\\\").replace("\"", "\\\"")
        buildConfigField("String", "LYRICS_URL", "\"$url\"")
        buildConfigField("boolean", "WEBVIEW_DEBUG", prop("lyrics.webviewDebug", "false"))
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Для установки по adb подпись debug-ключом достаточна.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}
