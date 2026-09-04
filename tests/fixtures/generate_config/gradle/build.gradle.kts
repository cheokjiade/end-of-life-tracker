plugins {
    java
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
    implementation(group = "com.google.guava", name = "guava", version = "33.2.1-jre")
    classpath("org.springframework.boot:spring-boot-gradle-plugin:3.3.4")
}
