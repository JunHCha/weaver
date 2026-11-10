plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// Release signing material lives outside the repository (docs/guides/android-signing.md).
// The keystore path may be overridden; passwords come only from environment variables.
val releaseKeystorePath: String = providers.environmentVariable("WEAVER_ANDROID_KEYSTORE")
    .orElse(providers.systemProperty("user.home").map { "$it/.config/weaver/android/weaver-release.p12" })
    .get()
val releaseKeystoreFile = file(releaseKeystorePath)
val releaseStorePassword: String? = providers.environmentVariable("WEAVER_ANDROID_KEYSTORE_PASSWORD").orNull
val releaseKeyAlias: String = providers.environmentVariable("WEAVER_ANDROID_KEY_ALIAS").orElse("weaver").get()
val releaseKeyPassword: String? = providers.environmentVariable("WEAVER_ANDROID_KEY_PASSWORD").orNull
    ?: releaseStorePassword

// Null when release signing is usable; otherwise the reason (never containing a secret).
val releaseSigningProblem: String? = when {
    !releaseKeystoreFile.isFile ->
        "Release keystore not found at $releaseKeystorePath. Create or restore it " +
            "(docs/guides/android-signing.md) or set WEAVER_ANDROID_KEYSTORE."
    releaseStorePassword.isNullOrEmpty() ->
        "WEAVER_ANDROID_KEYSTORE_PASSWORD is not set; release signing needs the keystore password."
    else -> null
}

// Packages the auto-share service observes: Voice Recorder, system share sheet and Weaver itself.
// Weaver's package differs per build type (debug adds a suffix), so the list is generated per build
// type into auto_share_service.xml; the Kotlin side uses the runtime packageName.
fun autoSharePackages(weaverPackage: String) =
    "com.sec.android.app.voicenote,com.android.intentresolver,$weaverPackage"

android {
    namespace = "io.github.junhcha.weaver"
    compileSdk = 36

    defaultConfig {
        applicationId = "io.github.junhcha.weaver"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        resValue("string", "auto_share_package_names", autoSharePackages("io.github.junhcha.weaver"))
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    signingConfigs {
        if (releaseSigningProblem == null) {
            create("release") {
                storeFile = releaseKeystoreFile
                storeType = "pkcs12"
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            resValue("string", "auto_share_package_names", autoSharePackages("io.github.junhcha.weaver.debug"))
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
        resValues = true
    }

    lint {
        warningsAsErrors = false
        abortOnError = true
        checkDependencies = true
    }

    testOptions {
        unitTests {
            isIncludeAndroidResources = true
            all { test ->
                val schemaDir = rootProject.layout.projectDirectory.dir("../schema")
                test.inputs.dir(schemaDir).withPropertyName("weaverSchema")
                test.systemProperty("weaver.schemaDir", schemaDir.asFile.absolutePath)
                val resDir = layout.projectDirectory.dir("src/main/res")
                test.inputs.dir(resDir).withPropertyName("weaverRes")
                test.systemProperty("weaver.resDir", resDir.asFile.absolutePath)
                // Rule snapshots live in the source tree so they can be refreshed in place:
                // ./gradlew testDebugUnitTest -Dweaver.updateRuleSnapshots=true
                test.systemProperty(
                    "weaver.ruleSnapshotDir",
                    layout.projectDirectory.dir("src/test/resources/schema-rules").asFile.absolutePath,
                )
                providers.systemProperty("weaver.updateRuleSnapshots").orNull?.let {
                    test.systemProperty("weaver.updateRuleSnapshots", it)
                    test.outputs.upToDateWhen { false }
                }
                // Robolectric's SDK 36 sandbox touches FileDescriptor internals via SharedSecrets.
                test.jvmArgs("--add-exports=java.base/jdk.internal.access=ALL-UNNAMED")
            }
        }
    }
}

// Fail release builds early and clearly instead of producing an unsigned or wrongly signed APK.
val verifyReleaseSigning = tasks.register("verifyReleaseSigning") {
    description = "Fails when the out-of-repo release signing key or its password is missing."
    val problem = releaseSigningProblem
    doLast {
        if (problem != null) throw GradleException(problem)
    }
}
tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    dependsOn(verifyReleaseSigning)
}

dependencies {
    implementation(libs.androidx.glance.appwidget)
    implementation(libs.androidx.work.runtime)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.kotlinx.serialization.json)
    debugImplementation(libs.androidx.compose.ui.tooling)
    debugImplementation(libs.androidx.compose.ui.test.manifest)

    androidTestImplementation(libs.androidx.test.ext.junit)
    androidTestImplementation(libs.androidx.test.core)
    androidTestImplementation(libs.androidx.test.runner)
    testImplementation(libs.androidx.glance.appwidget.testing)
    testImplementation(libs.androidx.work.testing)
    testImplementation(libs.junit)
    testImplementation(libs.json.schema.validator)
    testImplementation(libs.robolectric)
    testImplementation(libs.androidx.test.core)
    testImplementation(libs.androidx.test.ext.junit)
    testImplementation(platform(libs.androidx.compose.bom))
    testImplementation(libs.androidx.compose.ui.test.junit4)
}
