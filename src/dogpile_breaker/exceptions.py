class CacheError(Exception):
    pass


class CantDeserializeError(CacheError):
    pass


class CacheBackendInteractionError(CacheError):
    """Raised if cache backend not available"""
