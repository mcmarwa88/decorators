import functools
import pprint
import logging
from collections import namedtuple
import random
import time
import threading

def track_call(log=None):
    def decorator(f):
        argnames = f.func_code.co_varnames[:f.func_code.co_argcount]
        fname = f.func_name
        @functools.wraps(f)
        def track_func(*args, **kwargs):
            s = "%s : %s" % (fname, ', '.join('%s=%r' % entry for entry in zip(argnames, args) + kwargs.items()))
            if log: log.info(s)
            else: print s
            result = f(*args, **kwargs)
            if log: log.info("result:%s" % pprint.pformat(result))
            else: print "result:%s" % pprint.pformat(result)
            return result
        return track_func
    return decorator

def lock_call(lock):
    def decorator(f):
        @functools.wraps(f)
        def lock_func(*args, **kw):
            with lock:
                return f(*args, **kw)
        return lock_func
    return decorator

def time_call(log=None):
    def decorator(f):
        fname = f.func_name
        @functools.wraps(f)
        def time_func(*args, **kwargs):
            start = time.time()
            result = f(*args, **kwargs)
            elapsed = time.time() - start
            if log: log.info("%s elapsed time:%s" % (fname, elapsed))
            else: print "%s elapsed time:%s" % (fname, elapsed)
            return result
        return time_func
    return decorator


_CacheInfo = namedtuple("CacheInfo", ["hits", "misses", "maxsize", "currsize"])

class _HashedSeq(list):
    __slots__ = 'hashvalue'

    def __init__(self, tup, hash=hash):
        self[:] = tup
        self.hashvalue = hash(tup)

    def __hash__(self):
        return self.hashvalue

def _make_key(args, kwds, typed,
        kwd_mark = (object(),),
        fasttypes = {int, str, frozenset, type(None)},
        sorted=sorted, tuple=tuple, type=type, len=len):
    'Make a cache key from optionally typed positional and keyword arguments'
    key = args
    if kwds:
        sorted_items = sorted(kwds.items())
        key += kwd_mark
        for item in sorted_items:
            key += item
    if typed:
        key += tuple(type(v) for v in args)
        if kwds:
            key += tuple(type(v) for k, v in sorted_items)
    elif len(key) == 1 and type(key[0]) in fasttypes:
        return key[0]
    return _HashedSeq(key)

def lru_cache(maxsize=100, typed=False):
    """Least-recently-used cache decorator.

    If *maxsize* is set to None, the LRU features are disabled and the cache
    can grow without bound.

    If *typed* is True, arguments of different types will be cached separately.
    For example, f(3.0) and f(3) will be treated as distinct calls with
    distinct results.

    Arguments to the cached function must be hashable.

    View the cache statistics named tuple (hits, misses, maxsize, currsize) with
    f.cache_info().  Clear the cache and statistics with f.cache_clear().
    Access the underlying function with f.__wrapped__.

    See:  http://en.wikipedia.org/wiki/Cache_algorithms#Least_Recently_Used

    """

    # Users should only access the lru_cache through its public API:
    #       cache_info, cache_clear, and f.__wrapped__
    # The internals of the lru_cache are encapsulated for thread safety and
    # to allow the implementation to change (including a possible C version).

    def decorating_function(user_function):

        cache = dict()
        stats = [0, 0]                  # make statistics updateable non-locally
        HITS, MISSES = 0, 1             # names for the stats fields
        make_key = _make_key
        cache_get = cache.get           # bound method to lookup key or return None
        _len = len                      # localize the global len() function
        lock = threading.RLock()                  # because linkedlist updates aren't threadsafe
        root = []                       # root of the circular doubly linked list
        root[:] = [root, root, None, None]      # initialize by pointing to self
        nonlocal_root = [root]                  # make updateable non-locally
        PREV, NEXT, KEY, RESULT = 0, 1, 2, 3    # names for the link fields

        if maxsize == 0:

            def wrapper(*args, **kwds):
                # no caching, just do a statistics update after a successful call
                result = user_function(*args, **kwds)
                stats[MISSES] += 1
                return result

        elif maxsize is None:

            def wrapper(*args, **kwds):
                # simple caching without ordering or size limit
                key = make_key(args, kwds, typed)
                result = cache_get(key, root)   # root used here as a unique not-found sentinel
                if result is not root:
                    stats[HITS] += 1
                    return result
                result = user_function(*args, **kwds)
                cache[key] = result
                stats[MISSES] += 1
                return result

        else:

            def wrapper(*args, **kwds):
                # size limited caching that tracks accesses by recency
                key = make_key(args, kwds, typed) if kwds or typed else args
                with lock:
                    link = cache_get(key)
                    if link is not None:
                        # record recent use of the key by moving it to the front of the list
                        root, = nonlocal_root
                        link_prev, link_next, key, result = link
                        link_prev[NEXT] = link_next
                        link_next[PREV] = link_prev
                        last = root[PREV]
                        last[NEXT] = root[PREV] = link
                        link[PREV] = last
                        link[NEXT] = root
                        stats[HITS] += 1
                        return result
                result = user_function(*args, **kwds)
                with lock:
                    root, = nonlocal_root
                    if key in cache:
                        # getting here means that this same key was added to the
                        # cache while the lock was released.  since the link
                        # update is already done, we need only return the
                        # computed result and update the count of misses.
                        pass
                    elif _len(cache) >= maxsize:
                        # use the old root to store the new key and result
                        oldroot = root
                        oldroot[KEY] = key
                        oldroot[RESULT] = result
                        # empty the oldest link and make it the new root
                        root = nonlocal_root[0] = oldroot[NEXT]
                        oldkey = root[KEY]
                        oldvalue = root[RESULT]
                        root[KEY] = root[RESULT] = None
                        # now update the cache dictionary for the new links
                        del cache[oldkey]
                        cache[key] = oldroot
                    else:
                        # put result in a new link at the front of the list
                        last = root[PREV]
                        link = [last, root, key, result]
                        last[NEXT] = root[PREV] = cache[key] = link
                    stats[MISSES] += 1
                return result

        def cache_info():
            """Report cache statistics"""
            with lock:
                return _CacheInfo(stats[HITS], stats[MISSES], maxsize, len(cache))

        def cache_clear():
            """Clear the cache and cache statistics"""
            with lock:
                cache.clear()
                root = nonlocal_root[0]
                root[:] = [root, root, None, None]
                stats[:] = [0, 0]

        wrapper.__wrapped__ = user_function
        wrapper.cache_info = cache_info
        wrapper.cache_clear = cache_clear
        return functools.update_wrapper(wrapper, user_function)

    return decorating_function

@lru_cache(10)
def func_to_cache(a):
    time.sleep(0.1)
    return a*a

@track_call()
def func_to_track(a, b, c, x):
    return c + x * (b + x * a)

the_lock = threading.Lock()

@lock_call(the_lock)
def func_to_lock(a, t):
    print "%s sleeping %s" % (a, t)
    time.sleep(t)
    print "func done:%s" % a

@time_call()
def func_to_time(a, t):
    print "%s sleeping %s" % (a, t)
    time.sleep(t)
    print "func done:%s" % a

def thread1_func():
    for i in range(5):
        func_to_lock('thread1', 1.0)
        time.sleep(0.01)

def thread2_func():
    for i in range(5):
        func_to_lock('thread2', 1.1)
        time.sleep(0.01)

if __name__ == '__main__':
    func_to_track(2, 3, 4, 5)
    func_to_time('first', 2)
    func_to_time('second', 3)
    threading.Thread(target=thread1_func).start()
    threading.Thread(target=thread2_func).start()
#   for i in range(100):
#       func_to_cache(random.randint(0,9))
#       if i % 10 == 0:
#           print "cache info:%s" % pprint.pformat(func_to_cache.cache_info())
