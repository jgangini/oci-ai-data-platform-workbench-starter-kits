"""Small Autonomous document contract, separate from AIDP's conversation memory."""
from __future__ import annotations

import json
import re

DOCUMENT_NAME = re.compile(r"(?:configuration|simulation|reviews|runtime|status_[a-z]+|checkpoint_[a-z]+)")

TABLES = (
    "CREATE TABLE ADMIN.PRISMA_CONTROL_DOCS (name VARCHAR2(100) PRIMARY KEY, payload CLOB NOT NULL CHECK (payload IS JSON))",
    "CREATE TABLE ADMIN.PRISMA_PUBLICATIONS (version VARCHAR2(100) PRIMARY KEY, payload CLOB NOT NULL CHECK (payload IS JSON), published_at TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP)",
)

PACKAGE_SPEC = """CREATE OR REPLACE PACKAGE ADMIN.PRISMA_CONTROL AUTHID DEFINER AS
  FUNCTION READ_DOC(p_name VARCHAR2) RETURN CLOB;
  PROCEDURE WRITE_DOC(p_name VARCHAR2, p_document CLOB, p_expected NUMBER);
  PROCEDURE PUBLISH(p_version VARCHAR2, p_document CLOB);
END PRISMA_CONTROL;"""

PACKAGE_BODY = """CREATE OR REPLACE PACKAGE BODY ADMIN.PRISMA_CONTROL AS
  PROCEDURE valid_name(p_name VARCHAR2) IS BEGIN
    IF p_name IS NULL OR LENGTH(p_name) > 100 OR NOT REGEXP_LIKE(p_name, '^(configuration|simulation|reviews|runtime|status_[a-z]+|checkpoint_[a-z]+)$')
    THEN RAISE_APPLICATION_ERROR(-20002, 'Invalid PRISMA document'); END IF;
  END;
  FUNCTION READ_DOC(p_name VARCHAR2) RETURN CLOB IS v_doc CLOB;
  BEGIN
    valid_name(p_name);
    SELECT payload INTO v_doc FROM ADMIN.PRISMA_CONTROL_DOCS WHERE name = p_name;
    RETURN v_doc;
  EXCEPTION WHEN NO_DATA_FOUND THEN RETURN NULL;
  END;
  PROCEDURE WRITE_DOC(p_name VARCHAR2, p_document CLOB, p_expected NUMBER) IS v_revision NUMBER;
  BEGIN
    valid_name(p_name);
    v_revision := JSON_VALUE(p_document, '$.revision' RETURNING NUMBER NULL ON ERROR);
    IF p_document IS NULL OR p_expected IS NULL OR p_expected < 0 OR p_expected != TRUNC(p_expected)
       OR v_revision IS NULL OR v_revision != p_expected + 1
    THEN RAISE_APPLICATION_ERROR(-20002, 'Invalid PRISMA revision'); END IF;
    UPDATE ADMIN.PRISMA_CONTROL_DOCS SET payload = p_document
      WHERE name = p_name AND JSON_VALUE(payload, '$.revision' RETURNING NUMBER) = p_expected;
    IF SQL%ROWCOUNT = 0 THEN
      IF p_expected != 0 THEN RAISE_APPLICATION_ERROR(-20001, 'PRISMA revision conflict'); END IF;
      BEGIN
        INSERT INTO ADMIN.PRISMA_CONTROL_DOCS(name,payload) VALUES(p_name,p_document);
      EXCEPTION WHEN DUP_VAL_ON_INDEX THEN RAISE_APPLICATION_ERROR(-20001, 'PRISMA revision conflict'); END;
    END IF;
  END;
  PROCEDURE PUBLISH(p_version VARCHAR2, p_document CLOB) IS v_old CLOB; v_version VARCHAR2(100);
  BEGIN
    v_version := JSON_VALUE(p_document,'$.version' RETURNING VARCHAR2(100) NULL ON ERROR);
    IF p_version IS NULL OR p_document IS NULL OR LENGTH(p_version) > 100
       OR NOT REGEXP_LIKE(p_version, '^[A-Za-z0-9_-]+$') OR v_version IS NULL OR p_version != v_version
    THEN RAISE_APPLICATION_ERROR(-20002, 'Invalid publication'); END IF;
    BEGIN
      INSERT INTO ADMIN.PRISMA_PUBLICATIONS(version,payload) VALUES(p_version,p_document);
    EXCEPTION WHEN DUP_VAL_ON_INDEX THEN
      SELECT payload INTO v_old FROM ADMIN.PRISMA_PUBLICATIONS WHERE version=p_version;
      IF DBMS_LOB.COMPARE(v_old,p_document) != 0
      THEN RAISE_APPLICATION_ERROR(-20003, 'Immutable publication conflict'); END IF;
    END;
  END;
END PRISMA_CONTROL;"""

VIEWS = (
    "CREATE OR REPLACE VIEW ADMIN.PRISMA_V_SNAPSHOTS AS SELECT version,payload,published_at FROM ADMIN.PRISMA_PUBLICATIONS",
    """CREATE OR REPLACE VIEW ADMIN.PRISMA_V_INCIDENTS AS
       SELECT p.version,j.* FROM ADMIN.PRISMA_PUBLICATIONS p,
       JSON_TABLE(p.payload, '$.incidents[*]' COLUMNS (
         incident_id VARCHAR2(200) PATH '$.id', locality VARCHAR2(100) PATH '$.locality',
         category VARCHAR2(100) PATH '$.category', severity VARCHAR2(20) PATH '$.severity',
         mode VARCHAR2(20) PATH '$.mode', incident_json CLOB FORMAT JSON PATH '$')) j""",
    """CREATE OR REPLACE VIEW ADMIN.PRISMA_V_EVIDENCE AS
       SELECT p.version,j.* FROM ADMIN.PRISMA_PUBLICATIONS p,
       JSON_TABLE(p.payload, '$.evidence[*]' COLUMNS (
         evidence_id VARCHAR2(200) PATH '$.id', platform VARCHAR2(50) PATH '$.platform',
         mode VARCHAR2(20) PATH '$.mode', evidence_json CLOB FORMAT JSON PATH '$')) j""",
)


def install_schema(connection):
    cursor = connection.cursor()
    for statement in TABLES:
        try:
            cursor.execute(statement)
        except Exception as exc:
            if getattr(exc.args[0], "code", None) != 955:
                raise
    for statement in (PACKAGE_SPEC, PACKAGE_BODY, *VIEWS):
        cursor.execute(statement)
    cursor.execute("SELECT COUNT(*) FROM ALL_ERRORS WHERE OWNER='ADMIN' AND NAME='PRISMA_CONTROL'")
    if cursor.fetchone()[0]:
        raise RuntimeError("PRISMA database package compilation failed")
    cursor.execute("GRANT EXECUTE ON ADMIN.PRISMA_CONTROL TO AIDP_LAB_OPERATOR")
    connection.commit()


def read_document(connection, name: str) -> dict:
    import oracledb
    _valid_name(name)
    result = connection.cursor().callfunc("ADMIN.PRISMA_CONTROL.READ_DOC", oracledb.DB_TYPE_CLOB, [name])
    if result is None:
        return {"revision": 0}
    document = json.loads(result.read() if hasattr(result, "read") else result)
    if not isinstance(document, dict) or type(document.get("revision")) is not int or document["revision"] < 0:
        raise ValueError("Invalid PRISMA document revision")
    return document


def _valid_name(name):
    if not isinstance(name, str) or len(name) > 100 or not DOCUMENT_NAME.fullmatch(name):
        raise ValueError("Invalid PRISMA document name")


def write_document(connection, name: str, data: dict, expected: int) -> dict:
    _valid_name(name)
    if not isinstance(data, dict) or type(expected) is not int or expected < 0:
        raise ValueError("Invalid PRISMA document revision")
    document = {**data, "revision": expected + 1}
    serialized = json.dumps(document, ensure_ascii=False, allow_nan=False)
    connection.cursor().callproc("ADMIN.PRISMA_CONTROL.WRITE_DOC", [name, serialized, expected])
    return document


def mutate_document(connection, name: str, change) -> dict:
    current = read_document(connection, name)
    updated = change(dict(current))
    result = write_document(connection, name, updated, int(current.get("revision", 0)))
    connection.commit()
    return result


def publish(connection, snapshot: dict):
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("version"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", snapshot["version"])
            or not isinstance(snapshot.get("incidents"), list) or not isinstance(snapshot.get("evidence"), list)):
        raise ValueError("Invalid PRISMA publication")
    serialized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False)
    connection.cursor().callproc("ADMIN.PRISMA_CONTROL.PUBLISH",
        [snapshot["version"], serialized])
    connection.commit()
