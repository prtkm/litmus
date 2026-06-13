"""The audit pipeline (DESIGN §13) + the ExecutorAdapter seam (DESIGN §15)."""

from litmus.pipeline.executor import (
    ExecutorAdapter,
    LocalExecutor,
    ManagedAgentExecutor,
)

__all__ = ["ExecutorAdapter", "LocalExecutor", "ManagedAgentExecutor"]
