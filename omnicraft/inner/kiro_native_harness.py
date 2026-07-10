"""``harness: kiro-native`` wrap (the native Kiro TUI)."""

from __future__ import annotations

from fastapi import FastAPI

from omnicraft.inner.executor import Executor
from omnicraft.inner.kiro_native_executor import KiroNativeExecutor
from omnicraft.runtime.harnesses._executor_adapter import ExecutorAdapter


def _build_kiro_native_executor() -> Executor:
    """Construct a :class:`KiroNativeExecutor`."""
    return KiroNativeExecutor()


def create_app() -> FastAPI:
    """Build the kiro-native harness's FastAPI app (required entry point)."""
    adapter = ExecutorAdapter(executor_factory=_build_kiro_native_executor)
    return adapter.build()
