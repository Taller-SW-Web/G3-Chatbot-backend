"""Named cross-module capabilities (ADR-0024).

When the manifest DAG forbids a direct import, the owning module registers a
provider under one of these names in `configure()` and the consumer resolves it
at request time. Keeping the names here avoids magic strings and lets a module
refer to a capability without importing the module that owns it.
"""

CHECKOUT_DOCUMENT = "checkout_document"
CONVERSATION_CONTEXT = "conversation_context"
DISPATCH_PORT = "dispatch_port"
ORDER_CONFIRMATION = "order_confirmation"
OUTBOX_PROCESSOR = "outbox_processor"
# Per-type dispatch shared by the direct worker and the broker consumer (ADR-0030).
OUTBOX_TASK_HANDLER = "outbox_task_handler"
# Long-lived RabbitMQ publisher, registered by the worker lifespan in broker mode (ADR-0030).
OUTBOX_PUBLISHER = "outbox_publisher"
ACTION_SCOPE = "action_scope"
ATTACHMENT_CLEANUP = "attachment_cleanup"
FEEDBACK_NOTIFIER = "feedback_notifier"
CHECKOUT_EXPIRY = "checkout_expiry"

__all__ = [
    "ACTION_SCOPE",
    "ATTACHMENT_CLEANUP",
    "CHECKOUT_DOCUMENT",
    "CHECKOUT_EXPIRY",
    "CONVERSATION_CONTEXT",
    "DISPATCH_PORT",
    "FEEDBACK_NOTIFIER",
    "ORDER_CONFIRMATION",
    "OUTBOX_PROCESSOR",
    "OUTBOX_TASK_HANDLER",
    "OUTBOX_PUBLISHER",
]
