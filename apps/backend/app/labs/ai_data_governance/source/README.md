# Governance sources

This module reads the active AIDP Master Catalog through its signed APIs. It has
no synthetic input dataset. Generated catalog snapshots contain installation
metadata and stay in that installation's `oci_artifacts` Delta tables; they are
not exported into Git.

The checked-in [`../notebooks`](../notebooks) directory mirrors the uploaded
workspace filenames. The deployment fills only the declared `CONFIG = {}` block
with non-secret installation settings. Credential values stay in the shared
AIDP credential store. Notebook outputs are empty in Git; live run outputs stay
in AIDP.
