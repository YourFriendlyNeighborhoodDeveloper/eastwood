from prometheus_client import Counter, Histogram

events_in = Counter("eastwood_events_in_total", "Inbound events", ["topic"])
events_out = Counter("eastwood_events_out_total", "Outbound events", ["topic"])
dlq_total = Counter("eastwood_dlq_total", "DLQ events", ["reason"])
stage_time = Histogram("eastwood_stage_duration_seconds", "Stage duration", ["stage"])
