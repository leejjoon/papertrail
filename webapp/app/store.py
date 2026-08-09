"""Compatibility shim — migration Step 3. Real implementation moved to
src/papertrail/core/store.py. See webapp/app/registry.py's shim docstring."""
from papertrail.core.store import (  # noqa: F401
    add_message,
    create_answer,
    create_conversation,
    delete_conversation,
    get_answer,
    list_answers,
    list_conversations,
    list_messages,
    rename_conversation,
)
