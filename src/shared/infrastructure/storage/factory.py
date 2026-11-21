"""Chooses the attachment storage adapter from settings (`ATTACHMENT_STORAGE`)."""
from __future__ import annotations

from src.shared.application.ports.attachment_storage_port import AttachmentStoragePort
from src.shared.infrastructure.config.settings import Settings
from src.shared.infrastructure.storage.local_attachment_storage import LocalAttachmentStorage
from src.shared.infrastructure.storage.supabase_attachment_storage import SupabaseAttachmentStorage


def build_attachment_storage(settings: Settings) -> AttachmentStoragePort:
    if settings.attachment_storage == "supabase":
        return SupabaseAttachmentStorage(
            base_url=settings.supabase_url,
            service_key=settings.supabase_service_key,
            bucket=settings.attachment_bucket,
            public_base_url=settings.attachment_public_base_url,
            secret=settings.attachment_signing_secret,
            timeout_seconds=settings.attachment_storage_timeout_seconds,
        )
    return LocalAttachmentStorage(
        settings.attachment_storage_path,
        public_base_url=settings.attachment_public_base_url,
        secret=settings.attachment_signing_secret,
    )
