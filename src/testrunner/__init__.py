"""testrunner — a generic, asynchronous, state-machine test runner.

A subscriber exports actions, fixtures, observers and hooks with the decorators below;
the runner walks a plan of steps over them, checks every step against the declared
state model, and keeps no state of its own: everything persistent goes through the
subscriber's store.
"""
from .api import (ABORTED, ERRORED, FAILED, PASSED, SKIPPED, Outcome, OutcomeError,
                  Skip, action, fixture, hook, observer)

__version__ = "0.1.0.dev0"

__all__ = ["action", "fixture", "observer", "hook", "Outcome", "OutcomeError", "Skip",
           "PASSED", "FAILED", "SKIPPED", "ERRORED", "ABORTED", "__version__"]
