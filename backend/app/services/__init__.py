"""Backend services; independent workers must not load dashboard credentials."""

__all__ = ["VictoriaMetricsClient"]


def __getattr__(name: str):
    if name == "VictoriaMetricsClient":
        from .victoria import VictoriaMetricsClient

        return VictoriaMetricsClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
