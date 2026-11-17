package org.stopscroll.probe;

import android.content.ContentResolver;
import android.net.Uri;
import android.os.Process;
import android.os.SystemClock;
import android.util.Base64;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.InputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.PortUnreachableException;
import java.net.SocketTimeoutException;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Arrays;
import java.util.concurrent.Executor;
import java.util.function.Consumer;

final class ProbeOperation {
    private static final Uri PROVIDER = Uri.parse("content://com.celzero.bravedns.stopscroll.usb");
    private static final int UDP_INTERVAL_MS = 200;
    private static final int UDP_ECHO_TIMEOUT_MS = 1_000;
    private static final int UDP_WINDOW = UDP_ECHO_TIMEOUT_MS / UDP_INTERVAL_MS + 1;
    private static final int UDP_PAYLOAD_BYTES = 2 * Long.BYTES + Integer.BYTES;
    private static final int STUN_INTERVAL_MS = 1_000;
    private static final String DEFAULT_STUN_HOST = "stun.cloudflare.com";

    final String op;
    final long durationMs;
    private final String token;
    private final ContentResolver resolver;
    private final long startedElapsedMs = SystemClock.elapsedRealtime();
    private volatile boolean cancelled;
    private volatile Runnable cancelIo;
    private volatile Integer httpStatus;
    private volatile long bytes;
    private volatile int sent;
    private volatile int received;
    private volatile Long lastSuccessElapsedMs;

    ProbeOperation(ContentResolver resolver, String op, String token) {
        this.resolver = resolver;
        this.token = token;
        switch (op == null ? "" : op) {
            case "fetch":
            case "udp":
            case "stun":
                durationMs = 30_000;
                break;
            case "stream":
                durationMs = 60_000;
                break;
            case "provider":
                durationMs = 10_000;
                break;
            default:
                durationMs = 0;
        }
        this.op = durationMs == 0 ? "invalid" : op;
    }

    long remainingMs() {
        return startedElapsedMs + durationMs - SystemClock.elapsedRealtime();
    }

    void cancel(Executor closer) {
        cancelled = true;
        Runnable close = cancelIo;
        if (close != null) {
            closer.execute(close);
        }
    }

    JSONObject execute(String url, String host, int port, Consumer<JSONObject> progress) {
        try {
            switch (op) {
                case "fetch":
                case "stream":
                    return http(url);
                case "udp":
                    return udp(host, port, progress);
                case "stun":
                    return stun(host == null ? DEFAULT_STUN_HOST : host, port);
                case "provider":
                    return provider();
                default:
                    throw new IllegalArgumentException();
            }
        } catch (Exception e) {
            return result("error", e.getClass().getSimpleName());
        }
    }

    private JSONObject http(String url) throws IOException {
        if (url == null || url.isEmpty() || url.length() > 2_048) {
            throw new IllegalArgumentException();
        }
        URI uri = URI.create(url);
        if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                || uri.getHost() == null || uri.getRawUserInfo() != null || uri.getPort() > 65_535
                || uri.getPort() == 0) {
            throw new IllegalArgumentException();
        }

        HttpURLConnection connection = (HttpURLConnection) uri.toURL().openConnection();
        cancelIo = connection::disconnect;
        try {
            if (cancelled || remainingMs() <= 0) {
                return deadlineResult();
            }
            connection.setConnectTimeout(5_000);
            connection.setReadTimeout(10_000);
            connection.setUseCaches(false);
            connection.setInstanceFollowRedirects(false);
            connection.setRequestProperty("Connection", "close");
            connection.setRequestProperty("Accept-Encoding", "identity");
            httpStatus = connection.getResponseCode();
            try (InputStream body = httpStatus >= 400 ? connection.getErrorStream() : connection.getInputStream()) {
                if (body != null) {
                    byte[] buffer = new byte[16_384];
                    while (!cancelled && remainingMs() > 0) {
                        int count = body.read(buffer);
                        if (count < 0) {
                            break;
                        }
                        bytes += count;
                    }
                }
            }
            if (remainingMs() <= 0) {
                return deadlineResult();
            }
            return result("stream".equals(op) ? "eof" : "done", null);
        } finally {
            connection.disconnect();
            cancelIo = null;
        }
    }

    private JSONObject udp(String host, int port, Consumer<JSONObject> progress) throws IOException {
        if (host == null || host.isEmpty() || host.length() > 253 || port < 1 || port > 65_535) {
            throw new IllegalArgumentException();
        }
        InetAddress address = InetAddress.getByName(host);
        SecureRandom random = new SecureRandom();
        long nonceHigh = random.nextLong();
        long nonceLow = random.nextLong();
        ByteBuffer payload = ByteBuffer.allocate(UDP_PAYLOAD_BYTES);
        ByteBuffer echo = ByteBuffer.allocate(UDP_PAYLOAD_BYTES + 1);
        DatagramPacket outbound = new DatagramPacket(payload.array(), payload.capacity());
        DatagramPacket inbound = new DatagramPacket(echo.array(), echo.capacity());
        long[] pendingSentAt = new long[UDP_WINDOW];
        int[] pendingSequence = new int[UDP_WINDOW];
        Arrays.fill(pendingSequence, -1);
        int sequence = 0;
        long nextSend = SystemClock.elapsedRealtime();

        try (DatagramSocket socket = new DatagramSocket()) {
            cancelIo = socket::close;
            if (cancelled || remainingMs() <= 0) {
                return deadlineResult();
            }
            socket.connect(address, port);
            while (!cancelled && remainingMs() > 0) {
                long now = SystemClock.elapsedRealtime();
                if (now >= nextSend) {
                    payload.clear();
                    payload.putLong(nonceHigh).putLong(nonceLow).putInt(sequence);
                    try {
                        socket.send(outbound);
                        int slot = sequence % UDP_WINDOW;
                        pendingSequence[slot] = sequence;
                        pendingSentAt[slot] = now;
                        sent++;
                    } catch (PortUnreachableException ignored) {
                        // Continue periodic sends while the fixture is unreachable.
                    }
                    sequence++;
                    nextSend = now + UDP_INTERVAL_MS;
                }

                long timeout = Math.min(nextSend - SystemClock.elapsedRealtime(), remainingMs());
                if (timeout <= 0) {
                    continue;
                }
                socket.setSoTimeout((int) timeout);
                inbound.setLength(echo.capacity());
                try {
                    socket.receive(inbound);
                } catch (SocketTimeoutException | PortUnreachableException ignored) {
                    continue;
                }
                now = SystemClock.elapsedRealtime();
                echo.rewind();
                if (inbound.getLength() != UDP_PAYLOAD_BYTES || echo.getLong() != nonceHigh
                        || echo.getLong() != nonceLow) {
                    continue;
                }
                int echoed = echo.getInt();
                if (echoed < 0 || echoed >= sequence || remainingMs() <= 0) {
                    continue;
                }
                int slot = echoed % UDP_WINDOW;
                if (pendingSequence[slot] == echoed && now - pendingSentAt[slot] <= UDP_ECHO_TIMEOUT_MS) {
                    pendingSequence[slot] = -1;
                    lastSuccessElapsedMs = now;
                    received++;
                    bytes += UDP_PAYLOAD_BYTES;
                    JSONObject result = result("running", null);
                    put(result, "sequence", echoed);
                    put(result, "responseElapsedMs", now);
                    progress.accept(result);
                }
            }
        } finally {
            cancelIo = null;
        }
        return deadlineResult();
    }

    private JSONObject provider() {
        byte[] request = "{\"op\":\"status\"}".getBytes(StandardCharsets.UTF_8);
        String argument = Base64.encodeToString(request, Base64.NO_WRAP);
        try {
            resolver.call(PROVIDER, "rpc", argument, null);
            return result("error", "ExpectedSecurityException");
        } catch (SecurityException e) {
            return result("denied", e.getClass().getSimpleName());
        }
    }

    private JSONObject stun(String host, int port) throws IOException, InterruptedException {
        if (host.isEmpty() || host.length() > 253 || port < 1 || port > 65_535) {
            throw new IllegalArgumentException();
        }

        InetAddress address = InetAddress.getByName(host);
        SecureRandom random = new SecureRandom();
        byte[] reply = new byte[2_049];
        DatagramPacket inbound = new DatagramPacket(reply, reply.length);
        try (DatagramSocket socket = new DatagramSocket()) {
            cancelIo = socket::close;
            if (cancelled || remainingMs() <= 0) {
                return deadlineResult();
            }
            socket.connect(address, port);
            while (!cancelled && remainingMs() > 0) {
                long nextSend = SystemClock.elapsedRealtime() + STUN_INTERVAL_MS;
                byte[] request = StunBinding.request(random);
                try {
                    socket.send(new DatagramPacket(request, request.length));
                    sent++;
                    receiveStun(socket, inbound, request, nextSend);
                } catch (PortUnreachableException ignored) {
                    // Keep sampling on the original socket even when the path rejects packets.
                }
                long wait = Math.min(nextSend - SystemClock.elapsedRealtime(), remainingMs());
                if (!cancelled && wait > 0) {
                    Thread.sleep(wait);
                }
            }
        } finally {
            cancelIo = null;
        }
        return deadlineResult();
    }

    private void receiveStun(DatagramSocket socket, DatagramPacket inbound, byte[] request,
            long nextSend) throws IOException {
        while (!cancelled) {
            long timeout = Math.min(nextSend - SystemClock.elapsedRealtime(), remainingMs());
            if (timeout <= 0) {
                return;
            }
            socket.setSoTimeout((int) timeout);
            inbound.setLength(inbound.getData().length);
            try {
                socket.receive(inbound);
            } catch (SocketTimeoutException ignored) {
                return;
            }

            long now = SystemClock.elapsedRealtime();
            if (cancelled || now >= nextSend || remainingMs() <= 0) {
                return;
            }
            if (StunBinding.matches(request, inbound.getData(), inbound.getLength())) {
                received++;
                bytes += inbound.getLength();
                lastSuccessElapsedMs = now;
                return;
            }
        }
    }

    JSONObject deadlineResult() {
        return "udp".equals(op) || "stun".equals(op)
                ? result("done", null) : result("timeout", "DeadlineExceeded");
    }

    JSONObject result(String status, String error) {
        long now = SystemClock.elapsedRealtime();
        JSONObject result = new JSONObject();
        put(result, "op", op);
        put(result, "token", token);
        put(result, "uid", Process.myUid());
        put(result, "status", status);
        put(result, "error", error);
        put(result, "startedElapsedMs", startedElapsedMs);
        put(result, "finishedElapsedMs", "running".equals(status) ? null : now);
        put(result, "sampleElapsedMs", now);
        put(result, "elapsedMs", now - startedElapsedMs);
        put(result, "httpStatus", httpStatus);
        put(result, "bytes", bytes);
        put(result, "sent", sent);
        put(result, "received", received);
        put(result, "lastSuccessElapsedMs", lastSuccessElapsedMs);
        return result;
    }

    private static void put(JSONObject result, String key, Object value) {
        try {
            result.put(key, value == null ? JSONObject.NULL : value);
        } catch (JSONException e) {
            throw new AssertionError(e);
        }
    }
}
