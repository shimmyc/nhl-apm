"""Adjusted plus-minus from NHL public API data, 5v5 with both goalies in net.

Pipeline: fetch -> parse -> timeline -> stats -> output. Each stage is
independent of the team or season it is run for, so the same code extends to
all 32 clubs and, later, to a RAPM design matrix built off `timeline`.
"""

__version__ = "1.0.0"
