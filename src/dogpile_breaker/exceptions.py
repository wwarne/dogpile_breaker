class CacheError(Exception):
    pass


class CantDeserializeError(CacheError):
    """Raised if we can't deserialize data from cache via provided deserializer"""


class CacheBackendInteractionError(CacheError):
    """Raised if cache backend not available"""
