from redis import Redis


def build_redis_client(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, decode_responses=True)
