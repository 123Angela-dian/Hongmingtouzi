"""Small LangSmith compatibility layer.

The workflow can run before LangSmith is installed. Once `langsmith` is
available and the LANGSMITH_* environment variables are set, the same
functions become traceable without code changes.
"""

try:
    from langsmith import traceable as _traceable
except Exception:
    _traceable = None


def traceable_node(name):
    if _traceable is None:
        def decorator(func):
            return func
        return decorator
    return _traceable(name=name, run_type="chain", tags=["asset-radar"])


def flush_traces():
    try:
        from langsmith import wait_for_all_tracers
    except Exception:
        return
    wait_for_all_tracers()
