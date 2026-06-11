"""Resolve files and extensions to language profiles."""

import os

from .batch import BATCH
from .c_family import C_FAMILY
from .go import GO
from .hash_comments import HASH_COMMENTS
from .javascript import JAVASCRIPT, TYPESCRIPT
from .python import PYTHON
from .rust import RUST
from .unknown_language import UNKNOWN_LANGUAGE


_PROFILES = (
    PYTHON,
    C_FAMILY,
    RUST,
    GO,
    JAVASCRIPT,
    TYPESCRIPT,
    HASH_COMMENTS,
    BATCH,
)
_BY_EXTENSION = {
    extension: profile
    for profile in _PROFILES
    for extension in profile.extensions
}


def get_language_profile(file_path_or_extension):
    """Return the registered profile or the unknown-language fallback."""
    value = str(file_path_or_extension or "").replace("\\", "/")
    base_name = os.path.basename(value)
    if base_name.lower().startswith("makefile"):
        return HASH_COMMENTS

    if (
        value.startswith(".")
        and value.count(".") == 1
        and "/" not in value
    ):
        extension = value.lower()
    else:
        extension = os.path.splitext(value)[1].lower()
    return _BY_EXTENSION.get(extension, UNKNOWN_LANGUAGE)
