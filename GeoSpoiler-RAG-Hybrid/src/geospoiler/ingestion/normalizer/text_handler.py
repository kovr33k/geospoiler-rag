"""
Text Handler — normalizes plain text content from Telegram messages.
Strips Telegram formatting artifacts, excessive whitespace, and bot commands.
"""

import logging
import re

logger = logging.getLogger("geospoiler.normalizer.text")

# Patterns to clean up
_EXCESSIVE_NEWLINES = re.compile(r'\n{3,}')
_TELEGRAM_MARKUP = re.compile(r'[*_~`]{1,3}')  # Bold/italic/strikethrough markers
_BOT_COMMANDS = re.compile(r'(?<![\w/])/(\w+)(?:@\w+)?(?=\s|$)')
_HASHTAGS_ONLY_LINE = re.compile(r'^[\s#\w]+$')  # Lines that are ONLY hashtags


def normalize_text(raw_text: str) -> str:
    """
    Clean and normalize Telegram text.

    - Removes markdown formatting artifacts (* _ ~ `)
    - Removes bot command patterns (/start@bot)
    - Collapses excessive newlines (3+ → 2)
    - Strips leading/trailing whitespace
    - Preserves meaningful content, links, and structure
    """
    if not raw_text:
        return ""

    text = raw_text

    # Remove Telegram markdown-style formatting
    text = _TELEGRAM_MARKUP.sub("", text)

    # Remove bot commands
    text = _BOT_COMMANDS.sub("", text)

    # Collapse excessive newlines
    text = _EXCESSIVE_NEWLINES.sub("\n\n", text)

    # Strip each line
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(lines)

    # Final strip
    text = text.strip()

    return text
