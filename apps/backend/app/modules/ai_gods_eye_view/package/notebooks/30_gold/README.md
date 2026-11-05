# Gold publication

The publication stage in `../10_bronze/social_network.py` combines processed
social evidence and current sensors into a versioned snapshot. It exposes
`oci_gold.events`, `oci_gold.event_posts` and the JSON views
`oci_gold.territorial_incidents`, `oci_gold.territorial_evidence`,
`oci_gold.territorial_sensors` and `oci_gold.territorial_event_posts`.
The agent in `../40_report/ai_gods_eye_view.py` queries these Gold views using the
separate query compute. Existing publication versions and storage paths remain.
