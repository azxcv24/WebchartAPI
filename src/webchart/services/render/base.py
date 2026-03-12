"""Chart renderer protocol definition."""

from typing import Protocol, runtime_checkable

import pandas as pd

from webchart.models.chart import ChartSpec


@runtime_checkable
class ChartRenderer(Protocol):
    """Protocol for chart renderers."""

    def render(self, spec: ChartSpec, data: pd.DataFrame) -> bytes:
        """Render chart data to image bytes."""
        ...

    def supported_formats(self) -> list[str]:
        """List of supported output formats."""
        ...
