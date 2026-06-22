# Scoped Tools Example

This agent is a template, not a real pipeline step — it exists to show the
`tools:` scoping syntax in `agent.yaml`. See "Scoping `tools:` to specific tools"
in the README for the full explanation.

By default, listing a server name under `tools:` grants every tool that server
exposes. `agent.yaml` for this agent instead scopes the `grafana` entry down to
two specific tools (`search_dashboards`, `query_loki`) while still granting every
`filesystem` and `tavily` tool — both forms can be mixed in the same list.

This keeps the schema sent to the LLM smaller and the tool-call surface
narrower for servers (like Atlassian or Grafana) that expose many more tools
than a given agent actually needs.

---

## Workflow

1. Use the filesystem and search tools to gather whatever context the task asks for.
2. Use the two scoped Grafana tools only for dashboard search and Loki queries —
   no other Grafana tool is available to this agent.
3. Return the answer in the format the task prompt specifies.
