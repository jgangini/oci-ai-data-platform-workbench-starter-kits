# Colombia sensor simulation

This corpus contains **4,000 fictional sensor readings**, delivered as UTF-8 newline-delimited JSON in five `.txt` files. Every record carries `mode: Synthetic` and `is_simulated: true`. The stations, measurements and alert thresholds are simulated; none represents a real device or official warning.

The five folders contain river level (m), rainfall (mm/h), temperature (°C), soil moisture (%) and wind speed (km/h). Locations remain stable between batches around 40 approximate urban anchors across all 32 departments and Bogotá. Bogotá includes several localities for social-publication comparison. Observation times and delivery order vary; regional weather changes gradually.

Regenerate this example with `.venv/Scripts/python.exe scripts/generate_prisma_sensors.py`. `manifest.json` records file hashes and row counts. The example timestamp is fixed for reproducibility; it is not current telemetry.

In the application, **Sensors → Run now** delivers fresh files every five minutes to `01_landing/prisma/raw/sensors/<sensor_type>/<batch_id>.txt`. One file groups each type's readings per cycle. **Pause** stops future delivery without deleting history. AIDP uses its own streaming checkpoint, validates each line and merges immutable event IDs into `oci_medallion.oci_silver.sensor_events`, partitioned by UTC `event_date`. No sensor-ID or municipality partitions are created. Monitor daily partition sizes and compact small Delta files as the history grows.

The viewer and agent share a versioned publication containing the latest reading per sensor from the preceding 24 hours. Delta retains history. A comparison with social reports is a simulation exercise, never independent confirmation of a real emergency.
