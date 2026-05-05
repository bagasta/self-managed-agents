"""Prometheus custom metrics for the managed agent platform."""
from prometheus_client import Counter, Gauge, Histogram

agent_runs_total = Counter(
    "agent_runs_total",
    "Total agent runs",
    ["agent_id", "status"],  # status: success | error | timeout
)

agent_run_duration = Histogram(
    "agent_run_duration_seconds",
    "Duration of agent runs in seconds",
    ["agent_id"],
    buckets=[1, 5, 10, 30, 60, 120, 300],
)

llm_tokens_used = Counter(
    "llm_tokens_used_total",
    "Total LLM tokens consumed",
    ["agent_id", "model"],
)

sandbox_containers_active = Gauge(
    "sandbox_containers_active",
    "Number of active Docker sandbox containers",
)

scheduled_jobs_due = Gauge(
    "scheduled_jobs_due_total",
    "Number of scheduled jobs that are due but not yet executed",
)

wa_messages_received = Counter(
    "wa_messages_received_total",
    "WhatsApp messages received",
    ["device_id"],
)
