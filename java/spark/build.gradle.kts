dependencies {
    api(project(":core"))
    compileOnly("org.apache.spark:spark-sql_2.13:3.5.3")

    testImplementation(platform("org.junit:junit-bom:5.11.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
    testImplementation("org.wiremock:wiremock:3.10.0")
    // Spark local mode for integration tests (tagged "spark", excluded by default)
    testImplementation("org.apache.spark:spark-sql_2.13:3.5.3")
    testImplementation("org.apache.spark:spark-core_2.13:3.5.3")
}

tasks.withType<Test> {
    // Exclude @Tag("spark") tests unless -PsparkTests is passed
    if (!project.hasProperty("sparkTests")) {
        useJUnitPlatform {
            excludeTags("spark")
        }
    }
    jvmArgs("-Dio.netty.tryReflectionSetAccessible=true", "--add-opens=java.base/sun.nio.ch=ALL-UNNAMED")
}

configure<PublishingExtension> {
    publications {
        create<MavenPublication>("mavenJava") {
            from(components["java"])
            artifactId = "lakekeeper-client-spark-3.5_2.13"
            pom {
                name.set("Lakekeeper Java Client — Spark 3.5")
                description.set("Spark 3.5 integration for the Lakekeeper Java client")
                url.set("https://github.com/lakekeeper/lakekeeper-clients")
                licenses {
                    license {
                        name.set("Apache License, Version 2.0")
                        url.set("https://www.apache.org/licenses/LICENSE-2.0")
                    }
                }
                scm {
                    connection.set("scm:git:git://github.com/lakekeeper/lakekeeper-clients.git")
                    url.set("https://github.com/lakekeeper/lakekeeper-clients")
                }
            }
        }
    }
}
