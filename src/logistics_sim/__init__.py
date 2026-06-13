"""openddil-logistics-sim — per-element telemetry simulator for the
maintainer-tier 3D drill-down views.

Top-level framing: a logistics-domain sim that augments the customer
asset feeds (overlay proprietary, DIS, future) with synthetic sub-
component telemetry — health, temp, load, plus the upstream tx/rx
honor — that drives the maintainer's configuration-management and
prognostics decisions. First-shipped profile is MRAD; LTAMDS,
Patriot, and any other multi-element platform land as additional
config entries with no code change.
"""
__version__ = "0.1.0"
