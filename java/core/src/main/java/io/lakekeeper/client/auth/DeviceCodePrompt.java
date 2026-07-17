package io.lakekeeper.client.auth;

/**
 * What to show the user so they can approve a {@link DeviceCodeFlow} login.
 *
 * <p>{@code verificationUriComplete} (if the IdP provides it) already embeds {@code userCode} —
 * prefer it for a one-click / QR experience; otherwise show {@code verificationUri} and ask the
 * user to enter {@code userCode}.
 */
public final class DeviceCodePrompt {
    public final String verificationUri; // nullable
    public final String userCode; // nullable
    public final String verificationUriComplete; // nullable
    public final long expiresInSeconds;

    public DeviceCodePrompt(
            String verificationUri,
            String userCode,
            String verificationUriComplete,
            long expiresInSeconds) {
        this.verificationUri = verificationUri;
        this.userCode = userCode;
        this.verificationUriComplete = verificationUriComplete;
        this.expiresInSeconds = expiresInSeconds;
    }
}
