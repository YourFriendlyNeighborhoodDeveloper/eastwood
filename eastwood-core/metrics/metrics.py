from prometheus_client import Counter, Gauge, Histogram, Summary

leads_ingested = Counter(
    "eastwood_leads_ingested_total",
    "Total leads ingested",
    ["tenant_id", "source"],
)

leads_routed = Counter(
    "eastwood_leads_routed_total",
    "Total leads routed",
    ["tenant_id", "action", "band"],
)

agent_events_received = Counter(
    "eastwood_agent_events_received_total",
    "Total agent events received",
    ["event_type"],
)

worker_processing_seconds = Histogram(
    "eastwood_worker_processing_seconds",
    "Worker processing time",
    ["stage"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

active_dlq_messages = Gauge(
    "eastwood_active_dlq_messages",
    "Approximate number of messages in the DLQ topic",
)

http_requests_total = Counter(
    "eastwood_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
