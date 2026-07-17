// Single source of truth for the version: the release-please manifest, which release-please
// reliably bumps on every release (unlike gradle.properties via extra-files). The publish
// workflow checks out the release tag, at which commit the manifest holds the released version.
val projectVersion: String = file("../release-please/.release-please-manifest.json")
    .readText()
    .let { Regex("\"java\"\\s*:\\s*\"([^\"]+)\"").find(it)?.groupValues?.get(1) }
    ?: error("java version not found in release-please/.release-please-manifest.json")

allprojects {
    version = projectVersion
}

subprojects {
    apply(plugin = "java-library")
    apply(plugin = "maven-publish")

    repositories {
        mavenCentral()
    }

    configure<JavaPluginExtension> {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
        withSourcesJar()
        withJavadocJar()
    }

    tasks.withType<Test> {
        useJUnitPlatform()
        testLogging {
            events("passed", "skipped", "failed")
        }
    }

    configure<PublishingExtension> {
        repositories {
            maven {
                name = "GitHubPackages"
                url = uri("https://maven.pkg.github.com/lakekeeper/lakekeeper-clients")
                credentials {
                    username = System.getenv("GITHUB_ACTOR") ?: ""
                    password = System.getenv("GITHUB_TOKEN") ?: ""
                }
            }
        }
    }
}
