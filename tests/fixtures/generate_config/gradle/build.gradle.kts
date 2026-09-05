plugins {
    java
    id("org.jetbrains.kotlin.jvm") version "1.9.22"
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
    implementation(group = "com.google.guava", name = "guava", version = "33.2.1-jre")
    classpath("org.springframework.boot:spring-boot-gradle-plugin:3.3.4")
    implementation(platform("org.springframework.boot:spring-boot-dependencies:3.2.0"))
    implementation(libs.commons.lang3)
}

repositories {
    mavenCentral()
    maven { url = uri("https://gradle.example.invalid/m2") }
}
