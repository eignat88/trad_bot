"""Shadow scanner module for experimental signal collection."""

__all__ = [
    "ShadowScannerRunner",
    "ShadowBackfillRunner",
    "ShadowSignalEvaluator",
    "SRRResearchObserver",
    "SRRResearchEvaluator",
    "V2DRepository",
    "V2DRunner",
    "V2DEvaluator",
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
    if name == "V2DRepository":
        from .v2d_repository import V2DRepository
        return V2DRepository
    if name == "V2DRunner":
        from .v2d_runner import V2DRunner
        return V2DRunner
    if name == "V2DEvaluator":
        from .v2d_evaluator import V2DEvaluator
        return V2DEvaluator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
