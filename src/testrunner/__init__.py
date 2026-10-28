"""testrunner — a generic, asynchronous, state-machine test runner.

A subscriber exports actions, fixtures, observers and hooks with the decorators below;
the runner walks a plan of steps over them, checks every step against the declared
state model, and keeps no state of its own: everything persistent goes through the
subscriber's store.
"""
from .api import (ABORTED, CANNOT_TELL, DOES_NOT_HOLD, ERRORED, FAILED, HOLDS, NONE_HOLDS, PASSED, SKIPPED, Outcome,
                  OutcomeError, Skip, action, fixture, hook, observer, resolver, step_kind, validator,
                  validator_kind)

__version__ = "0.1.0.dev0"

__all__ = ["action", "fixture", "observer", "hook", "step_kind", "resolver", "validator", "validator_kind",
           "Outcome", "OutcomeError", "Skip", "PASSED", "FAILED", "SKIPPED", "ERRORED", "ABORTED",
           "HOLDS", "DOES_NOT_HOLD", "CANNOT_TELL", "NONE_HOLDS", "__version__"]
