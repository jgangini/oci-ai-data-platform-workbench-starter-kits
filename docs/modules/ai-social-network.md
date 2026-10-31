# AI Social Network — retired

[Documentation](../README.md) · [AI Market Intelligence](ai-market-intelligence.md)

The legacy package, ingestion installer and its exclusive runtime checks have
been removed. Existing authenticated API routes respond with HTTP 410 and cannot
install, redeploy, query or delete legacy resources. Updating the portal image
does not remove data or resources already present in a cloud workspace.

Install AI Market Intelligence separately through Settings → Application. It
starts with an empty framework model and paused research; it neither imports
Social history nor resumes its periodic ingestion workflow. God's Eye View has
its own social runtime and is unaffected by this source retirement.
