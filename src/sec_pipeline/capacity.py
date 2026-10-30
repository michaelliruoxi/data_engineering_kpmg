"""Database-size guard only; does not measure instance-wide free storage."""
GIB = 1024 ** 3
DEFAULT_MAX_DATABASE_GIB = 14


def database_limit_bytes(gib):
    if type(gib) is not int or gib < 1:
        raise ValueError('Database limit must be a positive integer GiB value')
    return gib * GIB


def database_at_limit(size_bytes, limit_bytes):
    if type(size_bytes) is not int or size_bytes < 0:
        raise ValueError('Observed database size must be nonnegative integer bytes')
    if type(limit_bytes) is not int or limit_bytes <= 0:
        raise ValueError('Database byte limit must be positive')
    return size_bytes >= limit_bytes
