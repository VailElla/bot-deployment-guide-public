"""Increase a retained provider's capacity on its event loop without draining it."""
from functools import wraps

LIMIT = 4


def _increase(provider, logger):
    current = getattr(provider, '_request_concurrency', 1)
    if current > LIMIT:
        raise RuntimeError('Cannot lower a live provider concurrency limit')
    # Releasing the extra permits retains the semaphore, its waiters and jobs.
    # No await occurs here: cancellation and admission remain on the same loop.
    for _ in range(LIMIT - current):
        provider._semaphore.release()
    provider._request_concurrency = LIMIT
    logger.info('[local_codex] concurrency_limit=4 retained_provider=true added_slots=%d', LIMIT - current)


def enable_four_requests(provider_class, providers, logger):
    if not getattr(provider_class, '_concurrency_init_supports_limit', False):
        original = provider_class.__init__

        @wraps(original)
        def initialize(self, *args, **kwargs):
            original(self, *args, **kwargs)
            _increase(self, logger)

        provider_class.__init__ = initialize
        provider_class._concurrency_init_supports_limit = True
    provider_class.REQUEST_CONCURRENCY = LIMIT
    for provider in providers:
        if isinstance(provider, provider_class):
            _increase(provider, logger)
