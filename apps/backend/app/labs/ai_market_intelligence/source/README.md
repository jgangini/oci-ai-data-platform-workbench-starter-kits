# AI Market Intelligence source policy

Installation loads no business data or demonstration records. The fourteen empty
framework tables and all 145 column descriptions are defined in
`../notebooks/agent/ai_market_intelligence/schema.json`. The agent obtains research
evidence only after its separately reviewed activation and budget assignment.

Runtime files live under `notebooks/agent/` and are explicitly declared with hashes
and workspace paths in `lab.json`. Host-side installation code belongs in
`app/market_installation/`; it is not uploaded into the agent workspace.
