package org.stopscroll.probe;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.AtomicFile;
import android.util.Log;
import android.view.WindowManager;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;

public final class ProbeActivity extends Activity {
    private static final String TAG = "StopscrollProbe";
    private static final ExecutorService WORKER = Executors.newSingleThreadExecutor();
    private static final ExecutorService CANCEL = Executors.newSingleThreadExecutor();

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable streamProgress = this::publishStreamProgress;
    private final Runnable deadline = this::finishAtDeadline;
    private AtomicFile resultFile;
    private TextView resultView;
    private ProbeOperation active;
    private Future<?> task;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setContentView(resultView = new TextView(this));
        File directory = getExternalFilesDir(null);
        if (directory != null) {
            resultFile = new AtomicFile(new File(directory, "result.json"));
        }
        start(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        start(intent);
    }

    @Override
    protected void onDestroy() {
        cancelActive();
        super.onDestroy();
    }

    private void start(Intent intent) {
        cancelActive();
        String op = intent.getStringExtra("op");
        String token = intent.getStringExtra("token");
        boolean validToken = token == null || token.matches("[A-Za-z0-9_.-]{1,96}");
        if (token == null || !validToken) {
            token = UUID.randomUUID().toString();
        }
        ProbeOperation operation = new ProbeOperation(getContentResolver(), op, token);
        if (!validToken || operation.durationMs == 0) {
            publish(operation.result("error", "IllegalArgumentException"));
            return;
        }
        if (resultFile == null) {
            publish(operation.result("error", "ExternalStorageUnavailable"));
            return;
        }

        String url = intent.getStringExtra("url");
        String host = intent.getStringExtra("host");
        int port = intent.getIntExtra("port", -1);
        active = operation;
        publish(operation.result("running", null));
        handler.postDelayed(deadline, Math.max(0, operation.remainingMs()));
        if ("stream".equals(operation.op)) {
            handler.postDelayed(streamProgress, 1_000);
        }
        task = WORKER.submit(() -> {
            JSONObject result = operation.execute(url, host, port, progress -> handler.post(() -> {
                if (active == operation) {
                    publish(progress);
                }
            }));
            handler.post(() -> {
                if (active == operation) {
                    clearActive();
                    publish(result);
                }
            });
        });
    }

    private void publishStreamProgress() {
        if (active != null) {
            publish(active.result("running", null));
            handler.postDelayed(streamProgress, 1_000);
        }
    }

    private void finishAtDeadline() {
        if (active != null) {
            stopActive(active.deadlineResult());
        }
    }

    private void cancelActive() {
        if (active != null) {
            stopActive(active.result("cancelled", null));
        }
    }

    private void stopActive(JSONObject result) {
        active.cancel(CANCEL);
        task.cancel(true);
        clearActive();
        publish(result);
    }

    private void clearActive() {
        active = null;
        task = null;
        handler.removeCallbacks(streamProgress);
        handler.removeCallbacks(deadline);
    }

    private void publish(JSONObject result) {
        String json = result.toString();
        Log.i(TAG, json);
        resultView.setText(json);
        if (resultFile == null) {
            return;
        }

        FileOutputStream writer = null;
        try {
            writer = resultFile.startWrite();
            writer.write((json + "\n").getBytes(StandardCharsets.UTF_8));
            resultFile.finishWrite(writer);
        } catch (IOException e) {
            resultFile.failWrite(writer);
            Log.e(TAG, "Result write failed: " + e.getClass().getSimpleName());
        }
    }
}
