package io.lakekeeper.example.flink;

import org.apache.flink.streaming.api.functions.source.RichSourceFunction;

import java.util.Random;

/**
 * Emits {@code batchSize} sensor readings, sleeps {@code intervalMs}, then repeats.
 * Wrap the emit loop in the checkpoint lock so a checkpoint always sees a complete batch.
 */
final class SensorBatchSource extends RichSourceFunction<SensorReading> {

    private static final String[] LOCATIONS =
            {"factory-floor-A", "factory-floor-B", "rooftop", "basement", "warehouse"};

    private final int  numSensors;
    private final int  batchSize;
    private final long intervalMs;
    private volatile boolean running = true;

    SensorBatchSource(int numSensors, int batchSize, long intervalMs) {
        this.numSensors  = numSensors;
        this.batchSize   = batchSize;
        this.intervalMs  = intervalMs;
    }

    @Override
    public void run(SourceContext<SensorReading> ctx) throws InterruptedException {
        long seq = 1;
        while (running) {
            synchronized (ctx.getCheckpointLock()) {
                for (int i = 0; i < batchSize; i++) {
                    ctx.collect(generate(seq++));
                }
            }
            Thread.sleep(intervalMs);
        }
    }

    @Override
    public void cancel() {
        running = false;
    }

    private SensorReading generate(long seq) {
        int    idx      = (int) ((seq - 1) % numSensors);
        Random rng      = new Random(seq);
        double temp     = 22.0 + idx * 1.5 + (rng.nextDouble() - 0.5) * 4.0;
        double humidity = 50.0 + idx * 2.0 + (rng.nextDouble() - 0.5) * 10.0;
        double pressure = 1013.0 + (rng.nextDouble() - 0.5) * 15.0;
        return new SensorReading(
                String.format("sensor-%03d", idx + 1),
                System.currentTimeMillis(),
                round2(temp),
                round2(Math.max(20, Math.min(95, humidity))),
                round2(pressure),
                LOCATIONS[idx % LOCATIONS.length]);
    }

    private static double round2(double v) {
        return Math.round(v * 100.0) / 100.0;
    }
}
