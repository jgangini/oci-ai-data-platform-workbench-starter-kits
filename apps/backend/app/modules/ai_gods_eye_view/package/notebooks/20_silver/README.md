# Silver processing

The Silver stages run inside the two files in `../10_bronze/`.
`social_network.py` normalizes and classifies captured publications, correlates
evidence and maintains `oci_silver.social_posts`, `oci_silver.events` and
`oci_silver.event_posts`. `sensor_stream.py` validates readings and maintains
`oci_silver.sensors_current`, retaining the latest reading for each sensor.
Existing physical tables and checkpoints are reused to preserve history.
