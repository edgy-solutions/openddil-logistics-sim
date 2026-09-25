"""
Releasability labelling for logistics-sim's published envelopes.

Loads the deployment's releasability declaration (see
`openddil-demo/ontology/releasability.yaml` for the rules this file
implements: per-asset declaration beats a document-wide default beats
a per-deployment `site_nation`; an asset that matches none of those
gets no label at all -- absence is deliberate, not an oversight). This
module is one of that document's declared consumers; it resolves an
`asset_id` to a `Label` (or `None`) for the publisher to attach to an
envelope, and does nothing else -- it does not decide what a consumer
downstream may do with the label.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("logistics_sim.releasability")


@dataclasses.dataclass(frozen=True)
class Label:
    originator_nation: str
    releasable_to: tuple[str, ...]


class ReleasabilityDeclaration:
    """Resolves asset_id -> Label per the precedence rules above.

    Construct via `load()` (normal path, file-backed) or
    `unavailable()` (file missing -- falls through to site_nation or
    to no label for every asset)."""

    def __init__(
        self,
        assets: dict[str, Label],
        default_originator_nation: str | None,
        site_nation: str | None,
        unavailable_reason: str | None = None,
    ) -> None:
        self._assets = assets
        self._default_originator_nation = default_originator_nation
        self._site_nation = site_nation or None
        self._unavailable_reason = unavailable_reason
        self._counts: dict[str, int] = {
            "declared": 0,
            "document_default": 0,
            "site_default": 0,
            "unlabelled": 0,
        }
        # Which asset ids have already gotten the once-per-asset
        # site_default warning, so a long-running tick loop doesn't
        # spam it every tick for the same undeclared asset.
        self._site_default_warned: set[str] = set()

    @classmethod
    def load(
        cls, path: str | os.PathLike[str], site_nation: str | None = None,
    ) -> "ReleasabilityDeclaration":
        try:
            with open(path, "rt") as f:
                raw: dict[str, Any] = yaml.safe_load(f) or {}
        except FileNotFoundError:
            log.warning("releasability declaration not found at %s; "
                        "assets will be unlabelled unless site_nation is set",
                        path)
            # Equivalent to unavailable(), but load()'s own site_nation
            # argument still applies -- unavailable() (the public,
            # reason-only factory) has no site_nation parameter, so we
            # build the object directly rather than losing it.
            return cls(
                assets={},
                default_originator_nation=None,
                site_nation=site_nation,
                unavailable_reason=f"file not found: {path}",
            )

        nations = set((raw.get("nations") or {}).keys())

        default_originator_nation = raw.get("default_originator_nation")
        if default_originator_nation is not None and default_originator_nation not in nations:
            raise ValueError(
                f"{path}: default_originator_nation {default_originator_nation!r} "
                f"is not in nations {sorted(nations)}"
            )

        if site_nation and site_nation not in nations:
            raise ValueError(
                f"{path}: site_nation {site_nation!r} is not in nations {sorted(nations)}"
            )

        assets: dict[str, Label] = {}
        for asset_id, row in (raw.get("assets") or {}).items():
            row = row or {}
            originator = row.get("originator_nation")
            if originator not in nations:
                raise ValueError(
                    f"{path}: asset {asset_id!r} has originator_nation "
                    f"{originator!r} which is not in nations {sorted(nations)}"
                )
            releasable_to = tuple(row.get("releasable_to") or ())
            for code in releasable_to:
                if code not in nations:
                    raise ValueError(
                        f"{path}: asset {asset_id!r} has releasable_to entry "
                        f"{code!r} which is not in nations {sorted(nations)}"
                    )
            assets[asset_id] = Label(
                originator_nation=originator, releasable_to=releasable_to,
            )

        # A DECLARATION THAT LABELS NOBODY IS A DEFECT, NOT A FLOOR.
        # Unlabelled is a legal answer -- that is deny-unlabeled working --
        # so a document that resolves to no labels at all produces a run
        # that passes while proving nothing. Compose did exactly that for as
        # long as it existed, against a zero-byte file created by a bind
        # mount, and nothing said a word. Refuse instead.
        #
        # A document-wide default labels every asset, so a file that sets
        # one and names no assets individually has declared something. Only
        # neither is a refusal. A MISSING file is a different fact and keeps
        # its warning above: a deployment may legitimately have no
        # declaration, but one that has authored a file has said it does.
        if not assets and not default_originator_nation:
            raise ValueError(
                f"{path}: the declaration is present and labels nobody -- "
                f"`assets` is empty and `default_originator_nation` is unset. "
                f"A declaration that resolves to zero labels makes every "
                f"published envelope unlabelled, which downstream cannot tell "
                f"from a fleet nobody ever declared."
            )

        return cls(
            assets=assets,
            default_originator_nation=default_originator_nation,
            site_nation=site_nation,
        )

    @classmethod
    def unavailable(cls, reason: str) -> "ReleasabilityDeclaration":
        """No document loaded (e.g. missing file). Empty asset map,
        no document default -- every asset falls through to
        site_nation, or to no label at all."""
        return cls(
            assets={},
            default_originator_nation=None,
            site_nation=None,
            unavailable_reason=reason,
        )

    def label_for(self, asset_id: str) -> Label | None:
        declared = self._assets.get(asset_id)
        if declared is not None:
            self._counts["declared"] += 1
            return declared

        if self._default_originator_nation is not None:
            self._counts["document_default"] += 1
            return Label(self._default_originator_nation, ())

        if self._site_nation:
            if asset_id not in self._site_default_warned:
                self._site_default_warned.add(asset_id)
                log.warning(
                    "asset %s has no releasability declaration; labelling "
                    "with the deployment's site_nation (%s) as a default, "
                    "not a declaration",
                    asset_id, self._site_nation,
                )
            self._counts["site_default"] += 1
            return Label(self._site_nation, ())

        self._counts["unlabelled"] += 1
        return None

    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def asset_count(self) -> int:
        """Number of assets named by the loaded declaration (0 for an
        unavailable declaration)."""
        return len(self._assets)
