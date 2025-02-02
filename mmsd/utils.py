# SPDX-License-Identifier: GPL-2.0
# Copyright (C) 2024 Bardia Moshiri <fakeshell@bardia.tech>

import asyncio

def async_retryable(times=0):
    """
    Decorator that allows to retry the given function n times.

    Usage:

    @async_retryable(5)
    async def fail():
        raise Exception("This function will be tried five times!")

    If times is 0 (default), the function will be retried indefinitely.
    """

    def decorator(func):
            async def wrapper(*args, **kwargs):
                    current_try = 0
                    while times == 0 or current_try < times:
                            try:
                                    result = await func(*args, **kwargs)
                            except Exception as e:
                                    if current_try == times-1:
                                            raise

                                    # print("Trying again, error was %s" % e)
                                    await asyncio.sleep(5)

                                    current_try += 1
                            else:
                                    return result

            return wrapper

    return decorator

def async_locked(func):
    async def wrapper(*args, **kwargs):
        async with func.__lock:
            return await func(*args, **kwargs)

    func.__lock = asyncio.Lock()
    return wrapper
