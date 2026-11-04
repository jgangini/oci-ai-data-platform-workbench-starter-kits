"""Read-only runtime readiness for the Governance migration; no credentials/data."""

import importlib.metadata
import json
import sys


def runtime_report(spark=None):
    versions = {}
    for package in ("aidputils", "langgraph", "langchain-core", "delta-spark", "pyspark", "pydantic"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    result = {"python": sys.version.split()[0], "packages": versions,
              "spark_session": spark is not None}
    if spark is not None:
        result["spark_version"] = spark.version
        result["artifacts_tables"] = [
            row.tableName for row in spark.sql("SHOW TABLES IN `oci_medallion`.`oci_artifacts`").collect()
            if row.tableName.casefold().startswith(("ai_dg_", "data_governance_"))
        ]
    return result


if __name__ == "__main__":
    print(json.dumps(runtime_report(globals().get("spark")), sort_keys=True))
