pluginManagement {
    repositories {
        // مرآة Google Android Maven — إن حُجب dl.google.com جرّب Gradle هنا أولاً
        maven { url = uri("https://maven.aliyun.com/repository/google") }
        maven { url = uri("https://dl.google.com/dl/android/maven2/") }
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven { url = uri("https://maven.aliyun.com/repository/google") }
        maven { url = uri("https://dl.google.com/dl/android/maven2/") }
        google()
        mavenCentral()
    }
}
rootProject.name = "NexoraTrade"
include(":app")
