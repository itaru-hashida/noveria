# cython: language_level=3
"""Native writer for the numeric enterprise planning view."""

def apply_outlook(object enterprise, object outlook):
    """Apply production_outlook fields in the same dict order as ``dict.update``."""
    cdef object key, value
    for key, value in outlook.items():
        enterprise[key] = value
    return enterprise
