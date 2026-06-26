dependencies {
    implementation("com.fasterxml.jackson.core:jackson-databind:2.17.2")

    testImplementation(platform("org.junit:junit-bom:5.11.3"))
    testImplementation("org.junit.jupiter:junit-jupiter")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
    testImplementation("org.wiremock:wiremock:3.10.0")
}

configure<PublishingExtension> {
    publications {
        create<MavenPublication>("mavenJava") {
            from(components["java"])
            artifactId = "lakekeeper-client-core"
            pom {
                name.set("Lakekeeper Java Client — Core")
                description.set("Pure-JVM generic-tables client and OAuth2 auth for Lakekeeper")
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
