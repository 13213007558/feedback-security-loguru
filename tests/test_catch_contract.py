"""Contract tests for ``logger.catch()``.

These tests pin down the exact catching contract of ``logger.catch()`` for all
supported callable kinds (plain functions, coroutines, generators and async
generators), at every execution phase where an exception may occur, and for
the interactions between the ``exception``, ``exclude``, ``reraise`` and
``default`` options.

The observable criteria are:

- the value (or exception) seen by the caller;
- the number of logged messages (exactly one per caught exception);
- the structure of propagated exception trees (notably for exception groups).

Exception groups are matched as a *whole*: the ``exception`` and ``exclude``
filters are only ever compared against the type of the raised group itself,
never against the types of the leaf exceptions it contains.
"""

import asyncio
import inspect
import sys

import pytest

from loguru import logger


def count_logs(writer):
    return len(writer.written)


# --------------------------------------------------------------------------------------
# Plain functions
# --------------------------------------------------------------------------------------


def test_function_success_returns_value_and_logs_nothing(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        return 42

    assert f() == 42
    assert count_logs(writer) == 0


def test_function_error_is_swallowed_and_returns_none_by_default(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        raise ValueError("boom")

    assert f() is None
    assert count_logs(writer) == 1


def test_function_error_returns_default(writer):
    logger.add(writer, format="{message}")

    @logger.catch(default="fallback")
    def f():
        raise ValueError("boom")

    assert f() == "fallback"
    assert count_logs(writer) == 1


def test_function_reraise_propagates_same_exception_and_logs_once(writer):
    logger.add(writer, format="{message}")
    error = ValueError("boom")

    @logger.catch(reraise=True, default="ignored")
    def f():
        raise error

    with pytest.raises(ValueError, match="boom") as exc_info:
        f()

    # The very same exception object is re-raised, and "default" is not returned.
    assert exc_info.value is error
    assert count_logs(writer) == 1


def test_function_exclude_propagates_without_logging(writer):
    logger.add(writer, format="{message}")

    @logger.catch(reraise=True, exclude=ValueError)
    def f():
        raise ValueError("excluded")

    # "exclude" wins over "reraise": the exception propagates and is never logged.
    with pytest.raises(ValueError, match="excluded"):
        f()

    assert count_logs(writer) == 0


def test_function_onerror_error_propagates_after_logging(writer):
    logger.add(writer, format="{message}")

    def onerror(_):
        raise RuntimeError("onerror failed")

    @logger.catch(onerror=onerror)
    def f():
        raise ValueError("original")

    # A failing "onerror" callback is not swallowed: its exception replaces the
    # (already logged) original one.
    with pytest.raises(RuntimeError, match="onerror failed"):
        f()

    assert count_logs(writer) == 1


def test_function_sink_error_while_logging_propagates(writer):
    def broken_sink(_):
        raise IOError("sink broken")

    logger.add(broken_sink, catch=False, format="{message}")

    @logger.catch
    def f():
        raise ValueError("original")

    # If logging itself fails, the sink error is not swallowed either.
    with pytest.raises(IOError, match="sink broken"):
        f()


# --------------------------------------------------------------------------------------
# BaseException-derived exceptions (cancellation, exit) are not caught by default
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("error", [KeyboardInterrupt(), SystemExit(3)])
def test_function_base_exception_propagates_without_logging(writer, error):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        raise error

    with pytest.raises(type(error)):
        f()

    assert count_logs(writer) == 0


def test_function_base_exception_caught_when_explicitly_opted_in(writer):
    logger.add(writer, format="{message}")

    @logger.catch(exception=BaseException, default="caught")
    def f():
        raise KeyboardInterrupt()

    assert f() == "caught"
    assert count_logs(writer) == 1


# --------------------------------------------------------------------------------------
# Coroutines
# --------------------------------------------------------------------------------------


def test_coroutine_error_at_await_time_not_call_time(writer):
    logger.add(writer, format="{message}")

    @logger.catch(default="fallback")
    async def f():
        raise ValueError("boom")

    coroutine = f()
    # Merely creating the coroutine does not execute anything: no log yet.
    assert count_logs(writer) == 0

    assert asyncio.run(coroutine) == "fallback"
    assert count_logs(writer) == 1


def test_coroutine_cancelled_error_propagates_without_logging(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    async def f():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(f())

    assert count_logs(writer) == 0


def test_coroutine_reraise(writer):
    logger.add(writer, format="{message}")

    @logger.catch(reraise=True)
    async def f():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        asyncio.run(f())

    assert count_logs(writer) == 1


def test_coroutine_introspection_preserved():
    @logger.catch
    async def f():
        return 1

    assert inspect.iscoroutinefunction(f)


# --------------------------------------------------------------------------------------
# Generators
# --------------------------------------------------------------------------------------


def test_generator_call_does_not_raise_nor_log(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        raise ValueError("boom")
        yield

    generator = f()
    # The body of a generator only runs when iterated: calling it is always safe.
    assert inspect.isgenerator(generator)
    assert count_logs(writer) == 0


def test_generator_error_before_first_yield_becomes_stop_iteration(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        raise ValueError("boom")
        yield

    with pytest.raises(StopIteration):
        next(f())

    assert count_logs(writer) == 1


def test_generator_error_mid_iteration_stops_iteration_and_returns_default(writer):
    logger.add(writer, format="{message}")

    @logger.catch(default=42)
    def f():
        yield 1
        raise ValueError("boom")
        yield 2  # Never reached: values after the error are lost.

    generator = f()
    assert next(generator) == 1

    # The caught error terminates the generator; "default" becomes the value
    # carried by StopIteration (i.e. the "return" value of the generator).
    with pytest.raises(StopIteration) as exc_info:
        next(generator)

    assert exc_info.value.value == 42
    assert count_logs(writer) == 1


def test_generator_error_thrown_in_is_caught(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        try:
            yield 1
        except KeyError:
            raise ValueError("converted") from None

    generator = f()
    next(generator)

    with pytest.raises(StopIteration):
        generator.throw(KeyError("thrown"))

    assert count_logs(writer) == 1


def test_generator_close_is_not_logged(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        yield 1
        yield 2

    generator = f()
    next(generator)
    # GeneratorExit raised by close() is a BaseException: not caught, not logged.
    generator.close()

    assert count_logs(writer) == 0


def test_generator_reraise_mid_iteration(writer):
    logger.add(writer, format="{message}")

    @logger.catch(reraise=True)
    def f():
        yield 1
        raise ValueError("boom")

    generator = f()
    assert next(generator) == 1

    with pytest.raises(ValueError, match="boom"):
        next(generator)

    assert count_logs(writer) == 1


def test_generator_introspection_preserved():
    @logger.catch
    def f():
        yield 1

    assert inspect.isgeneratorfunction(f)
    assert inspect.isgenerator(f())


# --------------------------------------------------------------------------------------
# Async generators
# --------------------------------------------------------------------------------------


def test_async_generator_error_before_first_yield_becomes_stop_async_iteration(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    async def f():
        raise ValueError("boom")
        yield

    async def main():
        async_generator = f()
        with pytest.raises(StopAsyncIteration):
            await async_generator.__anext__()

    asyncio.run(main())
    assert count_logs(writer) == 1


def test_async_generator_error_mid_iteration_ends_iteration_silently(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    async def f():
        yield 1
        raise ValueError("boom")
        yield 2  # Never reached.

    async def main():
        return [value async for value in f()]

    # The error is caught and simply terminates the "async for" loop.
    assert asyncio.run(main()) == [1]
    assert count_logs(writer) == 1


def test_async_generator_default_has_no_effect(writer):
    logger.add(writer, format="{message}")

    @logger.catch(default=42)
    async def f():
        yield 1
        raise ValueError("boom")

    async def main():
        async_generator = f()
        assert await async_generator.__anext__() == 1
        # Unlike plain generators, a stopped async generator cannot carry a
        # value: "default" is silently ignored and StopAsyncIteration is raised.
        with pytest.raises(StopAsyncIteration):
            await async_generator.__anext__()

    asyncio.run(main())
    assert count_logs(writer) == 1


def test_async_generator_error_thrown_in_is_caught(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    async def f():
        try:
            yield 1
        except KeyError:
            raise ValueError("converted") from None

    async def main():
        async_generator = f()
        await async_generator.__anext__()
        with pytest.raises(StopAsyncIteration):
            await async_generator.athrow(KeyError("thrown"))

    asyncio.run(main())
    assert count_logs(writer) == 1


def test_async_generator_aclose_is_not_logged(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    async def f():
        yield 1
        yield 2

    async def main():
        async_generator = f()
        await async_generator.__anext__()
        await async_generator.aclose()

    asyncio.run(main())
    assert count_logs(writer) == 0


def test_async_generator_reraise(writer):
    logger.add(writer, format="{message}")

    @logger.catch(reraise=True)
    async def f():
        yield 1
        raise ValueError("boom")

    async def main():
        async_generator = f()
        await async_generator.__anext__()
        with pytest.raises(ValueError, match="boom"):
            await async_generator.__anext__()

    asyncio.run(main())
    assert count_logs(writer) == 1


def test_async_generator_introspection_is_lost():
    @logger.catch
    async def f():
        yield 1

    # The decorated object is a plain function returning an object which merely
    # implements the async generator protocol: introspection is not preserved,
    # contrary to coroutines and generators.
    assert not inspect.isasyncgenfunction(f)
    assert not inspect.isasyncgen(f())
    assert hasattr(f(), "asend")
    assert hasattr(f(), "athrow")
    assert hasattr(f(), "aclose")


# --------------------------------------------------------------------------------------
# Exception groups (matched as a whole, never by leaf type)
# --------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_exception_group_caught_as_a_whole_by_default(writer):
    logger.add(writer, format="{message}")

    @logger.catch(default="fallback")
    def f():
        raise ExceptionGroup("group", [ValueError("leaf-1"), KeyError("leaf-2")])

    # ExceptionGroup is a subclass of Exception: the whole group is caught.
    assert f() == "fallback"
    assert count_logs(writer) == 1
    # The logged traceback preserves the group structure with its leaves.
    assert "ExceptionGroup" in writer.read()
    assert "ValueError: leaf-1" in writer.read()
    assert "KeyError: 'leaf-2'" in writer.read()


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_exception_group_not_matched_by_leaf_type(writer):
    logger.add(writer, format="{message}")

    @logger.catch(exception=ValueError)
    def f():
        raise ExceptionGroup("group", [ValueError("leaf")])

    # Matching applies to the group type itself, not to the leaves it contains.
    with pytest.raises(ExceptionGroup):
        f()

    assert count_logs(writer) == 0


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_exception_group_not_excluded_by_leaf_type(writer):
    logger.add(writer, format="{message}")

    @logger.catch(exclude=ValueError)
    def f():
        raise ExceptionGroup("group", [ValueError("leaf")])

    # "exclude" does not look inside the group either: the group is caught.
    assert f() is None
    assert count_logs(writer) == 1


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_exception_group_excluded_by_group_type(writer):
    logger.add(writer, format="{message}")

    @logger.catch(exclude=ExceptionGroup)
    def f():
        raise ExceptionGroup("group", [ValueError("leaf")])

    with pytest.raises(ExceptionGroup):
        f()

    assert count_logs(writer) == 0


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_base_exception_group_not_caught_by_default(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        raise BaseExceptionGroup("group", [KeyboardInterrupt(), ValueError("leaf")])

    # A group containing BaseException-only leaves is not an Exception subclass.
    with pytest.raises(BaseExceptionGroup):
        f()

    assert count_logs(writer) == 0


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_base_exception_group_caught_when_explicitly_opted_in(writer):
    logger.add(writer, format="{message}")

    @logger.catch(exception=BaseException, default="caught")
    def f():
        raise BaseExceptionGroup("group", [KeyboardInterrupt()])

    assert f() == "caught"
    assert count_logs(writer) == 1


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup requires Python 3.11+")
def test_nested_exception_groups_reraised_with_tree_intact(writer):
    logger.add(writer, format="{message}")
    inner = ExceptionGroup("inner", [ValueError("leaf-1")])
    outer = ExceptionGroup("outer", [inner, KeyError("leaf-2")])

    @logger.catch(reraise=True)
    def f():
        raise outer

    with pytest.raises(ExceptionGroup) as exc_info:
        f()

    # The whole group is re-raised untouched: the nested tree is preserved.
    assert exc_info.value is outer
    assert exc_info.value.exceptions[0] is inner
    (leaf,) = exc_info.value.exceptions[0].exceptions
    assert type(leaf) is ValueError
    assert leaf.args == ("leaf-1",)
    # Exactly one log for the whole tree, not one per leaf.
    assert count_logs(writer) == 1


# --------------------------------------------------------------------------------------
# Nested decorators: one log per catching layer
# --------------------------------------------------------------------------------------


def test_nested_decorators_inner_catch_hides_error_from_outer(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def inner():
        raise ValueError("boom")

    @logger.catch
    def outer():
        return inner()

    # The innermost layer swallows the error: the outer one sees a normal return.
    assert outer() is None
    assert count_logs(writer) == 1


def test_nested_decorators_reraise_logs_once_per_layer(writer):
    logger.add(writer, format="{message}")

    @logger.catch(reraise=True)
    def inner():
        raise ValueError("boom")

    @logger.catch(reraise=True)
    def outer():
        return inner()

    with pytest.raises(ValueError, match="boom"):
        outer()

    # Each "catch()" layer logs the exception it re-raises.
    assert count_logs(writer) == 2


# --------------------------------------------------------------------------------------
# Unsupported or misleading call forms (pinned behavior, see documentation)
# --------------------------------------------------------------------------------------


def test_sync_function_returning_coroutine_is_not_protected(writer):
    logger.add(writer, format="{message}")

    @logger.catch
    def f():
        async def inner():
            raise ValueError("boom")

        return inner()

    coroutine = f()
    # A plain function returning a coroutine is not an "async def": the "with"
    # block has already been left when the coroutine is awaited, so the error
    # propagates uncaught and unlogged.
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(coroutine)

    assert count_logs(writer) == 0


def test_decorating_classmethod_object_fails_at_call_time(writer):
    logger.add(writer, format="{message}")

    class Foo:
        @logger.catch
        @classmethod
        def method(cls):
            return 1

    # A "classmethod" object is not callable: it is misinterpreted as the
    # "exception" filter and the method is replaced by the catcher itself,
    # which fails with a confusing TypeError when called. The supported form
    # is to apply "@logger.catch" below "@classmethod".
    with pytest.raises(TypeError):
        Foo().method()


def test_decorating_below_staticmethod_and_classmethod_works(writer):
    logger.add(writer, format="{message}")

    class Foo:
        @staticmethod
        @logger.catch(default="static-fallback")
        def static():
            raise ValueError("boom")

        @classmethod
        @logger.catch(default="class-fallback")
        def klass(cls):
            raise ValueError("boom")

    assert Foo.static() == "static-fallback"
    assert Foo.klass() == "class-fallback"
    assert count_logs(writer) == 2
