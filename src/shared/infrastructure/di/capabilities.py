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
ACTION_SCOPE = "action_scope"
ATTACHMENT_CLEANUP = "attachment_cleanup"
FEEDBACK_NOTIFIER = "feedback_notifier"

__all__ = [
    "ACTION_SCOPE",
    "ATTACHMENT_CLEANUP",
    "CHECKOUT_DOCUMENT",
    "CONVERSATION_CONTEXT",
    "DISPATCH_PORT",
    "FEEDBACK_NOTIFIER",
    "ORDER_CONFIRMATION",
    "OUTBOX_PROCESSOR",
]
