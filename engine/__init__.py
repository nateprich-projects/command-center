"""engine — deterministic runners for the scheduled jobs (#794).

Each job gets a packet the runner assembles, one structured answer the model
returns, and effects the runner performs. This package imports ordering and
gates from funnel.py and never the reverse: funnel.py must not import engine.
"""
