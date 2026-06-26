package io.lakekeeper.example.flink;

import com.fasterxml.jackson.annotation.JsonProperty;

/** A single IoT sensor measurement. */
public final class SensorReading {
    @JsonProperty("sensor_id")    private String sensorId;
    @JsonProperty("timestamp_ms") private long   timestampMs;
    @JsonProperty("temperature")  private double temperature;  // °C
    @JsonProperty("humidity")     private double humidity;     // %
    @JsonProperty("pressure")     private double pressure;     // hPa
    @JsonProperty("location")     private String location;

    // Jackson needs a no-arg constructor
    public SensorReading() {}

    public SensorReading(String sensorId, long timestampMs, double temperature,
                         double humidity, double pressure, String location) {
        this.sensorId    = sensorId;
        this.timestampMs = timestampMs;
        this.temperature = temperature;
        this.humidity    = humidity;
        this.pressure    = pressure;
        this.location    = location;
    }

    public String getSensorId()    { return sensorId; }
    public long   getTimestampMs() { return timestampMs; }
    public double getTemperature() { return temperature; }
    public double getHumidity()    { return humidity; }
    public double getPressure()    { return pressure; }
    public String getLocation()    { return location; }
}
