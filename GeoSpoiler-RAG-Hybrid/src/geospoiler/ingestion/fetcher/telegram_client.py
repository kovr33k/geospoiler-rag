"""
Telegram Fetcher — downloads messages from all owned channels using Telethon.

Key features:
- Auto-discovers all channels where the user is admin/owner
- Handles forwards (extracts original source info)
- Groups album messages (multiple photos in one post)
- Downloads images to media_cache for Vision API processing
- Resumable via state.py (tracks last_message_id per channel)
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from telethon import TelegramClient
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import Channel, MessageMediaDocument, MessageMediaPhoto

from geospoiler import config
from geospoiler.ingestion.fetcher.state import get_last_message_id

logger = logging.getLogger("geospoiler.fetcher")


@dataclass
class TelegramMedia:
    """Metadata for an attached Telegram media item."""

    media_type: str
    mime_type: str = ""
    message_id: int = 0
    file_path: str = ""
    download_status: str = "not_attempted"
    error: str = ""


@dataclass
class TelegramMessage:
    """Normalized representation of a Telegram message (or grouped album)."""

    channel_name: str           # Human-readable title of OUR channel (e.g. "Russia")
    channel_id: int             # Telegram channel ID — stable, used as state key
    channel_username: str       # @username for public channels (empty string for private)
    message_id: int             # Message ID (highest in group for albums)
    date: datetime              # Message date (UTC)
    text: str                   # Combined text content
    # Forward info
    is_forward: bool = False
    forward_from_name: str = ""  # Original channel/user name
    forward_from_id: int = 0
    forward_date: datetime | None = None
    # Media
    image_paths: list[str] = field(default_factory=list)   # Local paths to downloaded images
    media: list[TelegramMedia] = field(default_factory=list)
    has_video: bool = False
    has_voice: bool = False
    has_document: bool = False
    # Links found in text
    urls: list[str] = field(default_factory=list)

    @property
    def post_url(self) -> str:
        """Direct link to this Telegram post.

        Public channel (has @username):  https://t.me/username/msg_id
        Private channel (no username):   https://t.me/c/channel_id/msg_id
        """
        # channel_username is the real @handle, not a numeric ID
        username = self.channel_username
        if username and not username.lstrip("-").isdigit():
            return f"https://t.me/{username}/{self.message_id}"
        # Private / no username — use numeric format
        return f"https://t.me/c/{self.channel_id}/{self.message_id}"


class TelegramFetcher:
    """Fetches messages from channels in the 'GeoSpoiler' Telegram folder."""

    # Name of the Telegram folder to read from (set in config or default)
    FOLDER_NAME = getattr(config, "TELEGRAM_FOLDER", "GeoSpoiler")

    def __init__(self):
        session_path = str(config.STATE_DIR / "telegram")
        self.client = TelegramClient(
            session_path,
            config.TELEGRAM_API_ID,
            config.TELEGRAM_API_HASH,
        )

    async def connect(self) -> None:
        """Connect and authenticate. Will prompt for phone/code on first run."""
        await self.client.start(phone=config.TELEGRAM_PHONE)
        logger.info("Telegram client connected.")

    async def disconnect(self) -> None:
        """Disconnect the client."""
        await self.client.disconnect()

    async def discover_channels(self) -> list[dict]:
        """
        Discover all channels inside the Telegram folder named FOLDER_NAME.
        Returns list of {id, name, title} dicts.

        Falls back to listing ALL available folders if the target folder is not found.
        """
        # Build a lookup: dialog_id -> dialog entity (for folder peer resolution)
        dialog_map: dict[int, object] = {}
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if hasattr(entity, "id"):
                dialog_map[entity.id] = entity

        # Get all folder filters from Telegram
        result = await self.client(GetDialogFiltersRequest())
        # result is a list of DialogFilter / DialogFilterDefault objects
        filters = result if isinstance(result, list) else result.filters

        folder_names = []
        target_filter = None
        for f in filters:
            title = getattr(f, "title", None)
            if title is None:
                continue
            # title may be a string or a TL object with 'text' attribute
            if hasattr(title, "text"):
                title = title.text
            folder_names.append(title)
            if title.strip().lower() == self.FOLDER_NAME.strip().lower():
                target_filter = f

        if target_filter is None:
            logger.error(
                f"Telegram folder '{self.FOLDER_NAME}' not found. "
                f"Available folders: {folder_names}. "
                f"Set TELEGRAM_FOLDER in .env to match one of these."
            )
            return []

        # Extract channel peers from the folder's include_peers list
        include_peers = getattr(target_filter, "include_peers", [])
        channels = []
        for peer in include_peers:
            # Peers can be InputPeerChannel, InputPeerChat, InputPeerUser, etc.
            peer_id = getattr(peer, "channel_id", None)
            if peer_id is None:
                continue
            entity = dialog_map.get(peer_id)
            if entity is None or not isinstance(entity, Channel):
                continue
            channels.append({
                "id": entity.id,
                "name": entity.username or str(entity.id),
                "title": entity.title,
                "access_hash": entity.access_hash,
            })
            logger.info(f"  Found channel in folder: {entity.title} (id={entity.id})")

        logger.info(
            f"Discovered {len(channels)} channel(s) in Telegram folder '{self.FOLDER_NAME}'."
        )
        return channels

    async def fetch_channel_messages(
        self,
        channel: dict,
        limit: int | None = None,
    ) -> list[TelegramMessage]:
        """
        Fetch all new messages from a channel (since last processed message).
        Messages are returned in chronological order (oldest first).
        """
        channel_name = channel["title"]
        channel_id = channel["id"]
        last_id = get_last_message_id(channel_id)

        logger.info(
            f"Fetching from '{channel_name}' (last_id={last_id})..."
        )

        raw_messages = []
        async for msg in self.client.iter_messages(
            channel["id"],
            min_id=last_id,
            limit=limit,
            reverse=True,  # oldest first for chronological processing
        ):
            raw_messages.append(msg)

        logger.info(f"  Got {len(raw_messages)} new messages from '{channel_name}'.")

        # Group album messages (same grouped_id)
        grouped = self._group_albums(raw_messages)

        result = []
        for group in grouped:
            tg_msg = await self._process_message_group(channel, group)
            if tg_msg:
                result.append(tg_msg)

        return result

    def _group_albums(self, messages: list) -> list[list]:
        """
        Group messages by grouped_id (albums).
        Single messages get their own group.
        """
        groups = []
        current_group = []
        current_group_id = None

        for msg in messages:
            gid = getattr(msg, "grouped_id", None)

            if gid is None:
                # Not part of an album
                if current_group:
                    groups.append(current_group)
                    current_group = []
                    current_group_id = None
                groups.append([msg])
            elif gid == current_group_id:
                # Same album
                current_group.append(msg)
            else:
                # New album
                if current_group:
                    groups.append(current_group)
                current_group = [msg]
                current_group_id = gid

        if current_group:
            groups.append(current_group)

        return groups

    async def _process_message_group(
        self,
        channel: dict,
        messages: list,
    ) -> TelegramMessage | None:
        """Process a single message or album group into a TelegramMessage."""
        # Use the first message for metadata (they share the same forward info in albums)
        first = messages[0]

        # Skip service messages (joins, pins, etc.)
        if first.action is not None:
            return None

        # Combine text from all messages in group
        texts = []
        for m in messages:
            if m.message:
                texts.append(m.message)

        combined_text = "\n".join(texts)

        # Build result
        tg_msg = TelegramMessage(
            channel_name=channel["title"],
            channel_id=channel["id"],
            channel_username=channel.get("name", "") or "",
            message_id=max(m.id for m in messages),
            date=first.date.replace(tzinfo=UTC) if first.date.tzinfo is None else first.date,
            text=combined_text,
        )

        # Forward info
        if first.forward:
            tg_msg.is_forward = True
            fwd = first.forward
            if fwd.chat:
                tg_msg.forward_from_name = getattr(fwd.chat, "title", "") or getattr(fwd.chat, "username", "") or ""
                tg_msg.forward_from_id = getattr(fwd.chat, "id", 0)
            elif fwd.sender:
                first_name = getattr(fwd.sender, "first_name", "") or ""
                last_name = getattr(fwd.sender, "last_name", "") or ""
                tg_msg.forward_from_name = f"{first_name} {last_name}".strip()
                tg_msg.forward_from_id = getattr(fwd.sender, "id", 0)
            elif fwd.from_name:
                tg_msg.forward_from_name = fwd.from_name
            tg_msg.forward_date = fwd.date

        # Process media for each message in group
        for m in messages:
            if isinstance(m.media, MessageMediaPhoto):
                # Download image
                img_path = await self._download_image(m, channel)
                if img_path:
                    tg_msg.image_paths.append(img_path)
                    tg_msg.media.append(TelegramMedia(
                        media_type="image",
                        message_id=m.id,
                        file_path=img_path,
                        download_status="downloaded",
                    ))
                else:
                    tg_msg.media.append(TelegramMedia(
                        media_type="image",
                        message_id=m.id,
                        download_status="failed",
                        error="download_failed",
                    ))
            elif isinstance(m.media, MessageMediaDocument):
                doc = m.media.document
                if doc:
                    mime = getattr(doc, "mime_type", "") or ""
                    media_type = _document_media_type(doc)
                    if media_type == "video":
                        tg_msg.has_video = True
                        tg_msg.media.append(await self._download_native_media(
                            m,
                            channel,
                            media_type=media_type,
                            mime_type=mime,
                        ))
                    elif media_type in {"audio", "voice"}:
                        tg_msg.has_voice = True
                        tg_msg.media.append(await self._download_native_media(
                            m,
                            channel,
                            media_type=media_type,
                            mime_type=mime,
                        ))
                    elif media_type == "image":
                        img_path = await self._download_image(m, channel)
                        if img_path:
                            tg_msg.image_paths.append(img_path)
                            tg_msg.media.append(TelegramMedia(
                                media_type="image",
                                mime_type=mime,
                                message_id=m.id,
                                file_path=img_path,
                                download_status="downloaded",
                            ))
                        else:
                            tg_msg.media.append(TelegramMedia(
                                media_type="image",
                                mime_type=mime,
                                message_id=m.id,
                                download_status="failed",
                                error="download_failed",
                            ))
                    else:
                        tg_msg.has_document = True
                        tg_msg.media.append(TelegramMedia(
                            media_type="document",
                            mime_type=mime,
                            message_id=m.id,
                            download_status="not_attempted",
                        ))

        # Extract URLs from text
        if combined_text:
            tg_msg.urls = config.WEB_URL_PATTERN.findall(combined_text)

        return tg_msg

    async def _download_native_media(
        self,
        message,
        channel: dict,
        media_type: str,
        mime_type: str = "",
    ) -> TelegramMedia:
        """Download native video/audio/voice media and return capture metadata."""
        record = TelegramMedia(
            media_type=media_type,
            mime_type=mime_type,
            message_id=message.id,
        )
        if not config.MEDIA_CAPTURE_ENABLED:
            record.download_status = "disabled"
            record.error = "MEDIA_CAPTURE_ENABLED=false"
            return record

        max_bytes = getattr(config, "MEDIA_CAPTURE_MAX_BYTES", 0)
        size = _message_file_size(message)
        if max_bytes > 0 and size is not None and size > max_bytes:
            record.download_status = "skipped"
            record.error = f"file_size_exceeds_limit:{size}>{max_bytes}"
            return record

        try:
            media_dir = config.MEDIA_CACHE_DIR / _sanitize(channel["title"]) / media_type
            media_dir.mkdir(parents=True, exist_ok=True)
            path = await message.download_media(
                file=str(media_dir / f"msg_{message.id}"),
            )
            if path:
                record.file_path = str(path)
                record.download_status = "downloaded"
                logger.debug(f"    Downloaded {media_type}: {path}")
            else:
                record.download_status = "failed"
                record.error = "download_media_returned_empty_path"
        except Exception as e:
            record.download_status = "failed"
            record.error = str(e)
            logger.warning(f"    Failed to download {media_type} msg_id={message.id}: {e}")
        return record

    async def _download_image(self, message, channel: dict) -> str | None:
        """Download an image from a message to media_cache/. Returns file path."""
        try:
            channel_dir = config.MEDIA_CACHE_DIR / _sanitize(channel["title"])
            channel_dir.mkdir(parents=True, exist_ok=True)
            path = await message.download_media(
                file=str(channel_dir / f"msg_{message.id}"),
            )
            if path:
                logger.debug(f"    Downloaded image: {path}")
                return path
        except Exception as e:
            logger.warning(f"    Failed to download image msg_id={message.id}: {e}")
        return None

    async def fetch_all_channels(
        self,
        limit_per_channel: int | None = None,
    ) -> dict[str, list[TelegramMessage]]:
        """
        Fetch new messages from ALL discovered channels.
        Returns {channel_title: [messages]}.
        """
        channels = await self.discover_channels()
        all_messages = {}

        for ch in channels:
            msgs = await self.fetch_channel_messages(ch, limit=limit_per_channel)
            all_messages[ch["title"]] = msgs

        return all_messages


def _sanitize(name: str) -> str:
    """Sanitize a string for use as a directory/file name."""
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in name).strip()


def _document_media_type(document) -> str:
    """Classify a Telegram document payload into the media type we track."""
    mime = getattr(document, "mime_type", "") or ""
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/") or "voice" in mime:
        for attr in getattr(document, "attributes", []) or []:
            if getattr(attr, "voice", False):
                return "voice"
        if "voice" in mime:
            return "voice"
        return "audio"
    return "document"


def _message_file_size(message) -> int | None:
    """Return a best-effort Telegram media size in bytes."""
    file_info = getattr(message, "file", None)
    size = getattr(file_info, "size", None)
    if size is None:
        document = getattr(getattr(message, "media", None), "document", None)
        size = getattr(document, "size", None)
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None
