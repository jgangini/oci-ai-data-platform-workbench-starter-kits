from types import SimpleNamespace

import pytest

from app.ai_data_governance.delta import bootstrap_source, create_statements, ensure_model, qualified_table, table_location
from app.ai_data_governance.schema import load_model


STORAGE_ROOT = "oci://selected_artifacts@examplenamespace/oci_artifacts"


class Frame:
    def __init__(self, value=None):
        self.value = value

    def collect(self):
        return self.value if isinstance(self.value, list) else []

    def first(self):
        return SimpleNamespace(asDict=lambda: self.value)


class Spark:
    def __init__(self, existing=(), changed=None):
        self.model = load_model()["tables"]
        self.existing = set(existing)
        self.changed = changed or {}
        self.executed = []
        self.catalog = SimpleNamespace(tableExists=lambda name: name in self.existing)

    def table(self, target):
        table = next(t for t in self.model if qualified_table(t["name"]) == target)
        fields = [SimpleNamespace(
            name=c["name"], nullable=not c["required"],
            dataType=SimpleNamespace(typeName=lambda typ=c["type"]: typ.lower()),
            metadata={"comment": c["comment"]},
        ) for c in table["columns"]]
        if self.changed.get("column"):
            fields[0].nullable = not fields[0].nullable
        return SimpleNamespace(schema=SimpleNamespace(fields=fields))

    def sql(self, sql):
        self.executed.append(sql)
        if sql.startswith("DESCRIBE DETAIL "):
            raise RuntimeError("AIDP Delta DETAIL resolved the namespace through Iceberg")
        if sql.startswith("DESCRIBE TABLE EXTENDED "):
            target = sql.removeprefix("DESCRIBE TABLE EXTENDED ")
            table = next(t for t in self.model if qualified_table(t["name"]) == target)
            data = {"format": "delta", "partitionColumns": [],
                    "description": table["grain"], "type": "EXTERNAL", "location": table_location(STORAGE_ROOT, table["name"]), **self.changed}
            rows = [(c["name"], c["type"].lower()) for c in table["columns"]]
            if data["partitionColumns"]:
                rows.append(("# Partition Information", ""))
            rows.extend([("# Detailed Table Information", ""), ("Provider", data["format"]),
                         ("Type", data["type"]), ("Location", data["location"]), ("Comment", data["description"])])
            return Frame([SimpleNamespace(col_name=key, data_type=value) for key, value in rows])
        return Frame()


def test_ddl_has_all_descriptions_without_keys_partitions_or_destructive_sql():
    statements = create_statements(STORAGE_ROOT)
    assert len(statements) == 18
    assert sum(sql.count(" COMMENT ") for sql in statements) == 192
    for sql in statements:
        assert sql.startswith("CREATE TABLE IF NOT EXISTS `oci_medallion`.`oci_artifacts`.`AI_DG_")
        assert "USING DELTA\nLOCATION " in sql
        assert "\nCOMMENT " in sql
        assert STORAGE_ROOT in sql
        assert not any(token in sql for token in ("DROP ", "TRUNCATE ", "INSERT ", "PARTITIONED", "PRIMARY KEY", "FOREIGN KEY"))


@pytest.mark.parametrize("name", ["ai_mi_sources", "data_governance_metadata", "AI_DG_SCOPE` DROP TABLE x"])
def test_arbitrary_table_names_cannot_enter_sql(name):
    with pytest.raises(ValueError, match="Unknown"):
        qualified_table(name)


def test_empty_schema_creates_and_verifies_all_tables():
    spark = Spark()
    result = ensure_model(spark, STORAGE_ROOT)
    assert len(result["created"]) == 18
    assert result["verified_existing"] == []
    assert sum(s.startswith("CREATE TABLE") for s in spark.executed) == 18
    assert sum(s.startswith("DESCRIBE TABLE EXTENDED") for s in spark.executed) == 18


def test_rerun_verifies_without_writing():
    spark = Spark(qualified_table(t["name"]) for t in load_model()["tables"])
    assert len(ensure_model(spark, STORAGE_ROOT)["verified_existing"]) == 18
    assert not any(s.startswith("CREATE") for s in spark.executed)


@pytest.mark.parametrize("changed", [{"format": "parquet"}, {"partitionColumns": ["run_id"]},
                                     {"description": "Existing human description"}, {"column": True},
                                     {"type": "MANAGED"}, {"location": "oci://warehouse@examplenamespace/catalog.db/table"}])
def test_incompatible_existing_table_stops_before_any_mutation(changed):
    spark = Spark([qualified_table("AI_DG_SCOPE")], changed)
    with pytest.raises(ValueError):
        ensure_model(spark, STORAGE_ROOT)
    assert not any(s.startswith("CREATE") for s in spark.executed)


def test_workspace_script_is_standalone_and_preserves_the_approved_contract():
    source = bootstrap_source(STORAGE_ROOT)
    namespace = {"__name__": "bootstrap_test"}
    exec(compile(source, "create_model.py", "exec"), namespace)
    assert namespace["MODEL"] == load_model()
    assert namespace["create_statements"](STORAGE_ROOT) == create_statements(STORAGE_ROOT)
    spark = Spark(qualified_table(t["name"]) for t in load_model()["tables"])
    assert namespace["ensure_model"](spark, STORAGE_ROOT)["created"] == []
    assert not any(s.startswith("CREATE") for s in spark.executed)
    assert "base64" not in source.decode("utf-8")


def test_partial_creation_can_resume_without_replacing_previously_created_tables():
    class InterruptedSpark(Spark):
        def sql(self, sql):
            if sql.startswith("CREATE TABLE"):
                if len(self.existing) == 1 and not getattr(self, "recovered", False):
                    raise TimeoutError("DDL response unavailable")
                self.existing.add(sql.split(" (", 1)[0].removeprefix("CREATE TABLE IF NOT EXISTS "))
            return super().sql(sql)

    spark = InterruptedSpark()
    with pytest.raises(TimeoutError):
        ensure_model(spark, STORAGE_ROOT)
    assert spark.existing == {qualified_table("AI_DG_SCOPE")}
    spark.recovered = True
    result = ensure_model(spark, STORAGE_ROOT)
    assert result["verified_existing"] == ["AI_DG_SCOPE"]
    assert len(result["created"]) == 17
    assert sum(s.startswith("CREATE TABLE IF NOT EXISTS " + qualified_table("AI_DG_SCOPE") + " (")
               for s in spark.executed) == 1


def test_concurrent_incompatible_creator_does_not_count_as_success():
    class RaceSpark(Spark):
        def sql(self, sql):
            if sql.startswith("CREATE TABLE"):
                self.changed["description"] = "A concurrent creator wrote different metadata"
            return super().sql(sql)

    spark = RaceSpark()
    with pytest.raises(ValueError, match="documentation differs"):
        ensure_model(spark, STORAGE_ROOT)
    assert sum(s.startswith("CREATE TABLE") for s in spark.executed) == 1


@pytest.mark.parametrize("storage_root", [None, "", "oci://bucket@namespace/", "oci://bucket@namespace/oci_artifacts/",
                                         "oci://bucket@namespace/oci_artifacts' DROP TABLE x",
                                         "file:///warehouse/oci_artifacts"])
def test_invalid_storage_cannot_trigger_catalog_reads_or_writes(storage_root):
    spark = Spark()
    with pytest.raises(ValueError, match="explicit artifacts"):
        ensure_model(spark, storage_root)
    assert spark.executed == []


def test_default_managed_warehouse_is_never_accepted_as_artifacts_storage():
    spark = Spark([qualified_table("AI_DG_SCOPE")], {"type": "MANAGED",
                  "location": "oci://IDL-warehouse@namespace/warehouse/oci_artifacts.db/id"})
    with pytest.raises(ValueError, match="storage differs"):
        ensure_model(spark, STORAGE_ROOT)
    assert not any(s.startswith("CREATE") for s in spark.executed)
