"""Redis connection pool management."""

from redis.asyncio import Redis, from_url


async def create_redis_pool(redis_url: str) -> Redis:
    """Create and verify an async Redis connection pool."""
    pool: Redis = from_url(redis_url, decode_responses=True)  # type: ignore[no-untyped-call]
    await pool.ping()
    return pool


async def close_redis_pool(pool: Redis) -> None:
    """Gracefully close the Redis connection pool."""
    await pool.aclose()


async def check_redis_health(pool: Redis) -> bool:
    """Return True if Redis is reachable."""
    try:
        result: bool = await pool.ping()
        return result
    except Exception:
        return False
