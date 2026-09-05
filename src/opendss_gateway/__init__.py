"""opendss-designer-gateway: the hosted-service front for OpenDSS Designer.

Workers are unmodified ``opendss-designer`` containers, one engine each. This
gateway is the only thing that knows who a caller is: it picks a plan, holds
one dispatch slot per worker, tightens the worker's limits per request through
the trusted limits header, and meters the engine-seconds each call used.
"""

__version__ = "0.1.0"
