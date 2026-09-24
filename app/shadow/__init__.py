"""Shadow scanner module for experimental signal collection."""

__all__ = [
    "ShadowScannerRunner",
    "ShadowBackfillRunner",
    "ShadowSignalEvaluator",
    "SRRResearchObserver",
    "SRRResearchEvaluator",
]


def __getattr__(name: str):
    if name == "ShadowScannerRunner":
        from .runner import ShadowScannerRunner
        return ShadowScannerRunner
    if name == "ShadowBackfillRunner":
        from .backfill import ShadowBackfillRunner
        return ShadowBackfillRunner
    if name == "ShadowSignalEvaluator":
        from .evaluator import ShadowSignalEvaluator
        return ShadowSignalEvaluator
    if name == "SRRResearchObserver":
        from .srr_research_observer import SRRResearchObserver
        return SRRResearchObserver
    if name == "SRRResearchEvaluator":
        from .srr_research_evaluator import SRRResearchEvaluator
        return SRRResearchEvaluator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
