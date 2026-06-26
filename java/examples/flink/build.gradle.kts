plugins {
    id("com.github.johnrengelman.shadow") version "8.1.1"
    application
}

val flinkVersion = "1.19.1"

dependencies {
    implementation(project(":core"))
    implementation("com.fasterxml.jackson.core:jackson-databind:2.17.2")

    // Flink runtime — provided at cluster time, included in the fat JAR for local runs
    compileOnly("org.apache.flink:flink-streaming-java:$flinkVersion")
    compileOnly("org.apache.flink:flink-clients:$flinkVersion")
    compileOnly("org.apache.flink:flink-connector-files:$flinkVersion")

    // Include Flink in the fat JAR so the example runs standalone via `./gradlew run`
    runtimeOnly("org.apache.flink:flink-streaming-java:$flinkVersion")
    runtimeOnly("org.apache.flink:flink-clients:$flinkVersion")
    runtimeOnly("org.apache.flink:flink-connector-files:$flinkVersion")
    // S3 filesystem plugin — registered via ServiceLoader when on the classpath
    runtimeOnly("org.apache.flink:flink-s3-fs-hadoop:$flinkVersion")
    // Suppress the SLF4J NOP warning
    runtimeOnly("org.slf4j:slf4j-simple:1.7.36")
}

application {
    mainClass.set("io.lakekeeper.example.flink.IotStreamJob")
}

tasks.shadowJar {
    archiveClassifier.set("all")
    mergeServiceFiles()
    // Exclude Flink's bundled Hadoop to avoid conflicts when submitting to a cluster
    exclude("META-INF/*.SF", "META-INF/*.DSA", "META-INF/*.RSA")
}

// No publications — this is an example, not a library artifact

// Load .env.local from the repo root into the `run` task automatically.
// Create java/.env.local (git-ignored) with KEY=value lines.
tasks.named<JavaExec>("run") {
    val envFile = rootProject.file(".env.local")
    if (envFile.exists()) {
        envFile.readLines()
            .filter { it.isNotBlank() && !it.startsWith("#") && it.contains("=") }
            .forEach { line ->
                val (key, value) = line.split("=", limit = 2)
                environment(key.trim(), value.trim())
            }
    }
}
