#!/usr/bin/env python3
"""Detect newer versions of the models the Command Center actually uses.

The release watch is deliberately a read-only check.  It observes the models
recorded by :mod:`heartbeat`, compares them with a captured vendor model
catalogue, and hands the resulting task descriptions to the caller.  The
nightly job owns the transport that turns those descriptions into TickTick
tasks; this module never edits a model, a settings file, or any other
configuration.

Keeping the comparison pure is important here.  A new release is a fact to
surface, not permission to change the engine that writes or reviews code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import heartbeat


class CouldNotCheck(ValueError):
    """A vendor response or model identity was not safe to interpret."""


@dataclass(frozen=True)
class TickTickTask:
    """The complete information needed to create one notification task."""

    provider: str
    current_model: str
    newer_model: str

    @property
    def title(self) -> str:
        return "New {} model version: {}".format(self.provider, self.newer_model)

    @property
    def content(self) -> str:
        return (
            "{} is using {}. Vendor catalogues now include {}. "
            "Review the release; this watch never changes configuration."
        ).format(self.provider, self.current_model, self.newer_model)

    def payload(self, project_id: str) -> Dict[str, Any]:
        """Return the documented task fields without performing a network write."""
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("a TickTick project id is required")
        return {
            "projectId": project_id,
            "title": self.title,
            "content": self.content,
        }


@dataclass(frozen=True)
class WatchFault:
    """A check failure that must be surfaced rather than treated as silence."""

    provider: str
    reason: str
    kind: str = "could-not-check"

    def __str__(self) -> str:
        return "{}: {} ({})".format(self.kind, self.provider, self.reason)


@dataclass(frozen=True)
class WatchResult:
    """The changes and faults found by one release-watch pass."""

    notifications: Tuple[TickTickTask, ...] = ()
    faults: Tuple[WatchFault, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.notifications and not self.faults


# Provider responses commonly expose a model list under one of these names.
# The parser is intentionally narrow: accepting an unknown payload as an empty
# catalogue would turn a vendor layout change into false silence.
CATALOG_KEYS = ("data", "models", "items", "results")
MODEL_ID_KEYS = ("id", "name", "model")


def _json_payload(response: Any) -> Any:
    if isinstance(response, bytes):
        try:
            response = response.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CouldNotCheck("response is not UTF-8") from exc
    if isinstance(response, str):
        try:
            return json.loads(response)
        except (TypeError, ValueError) as exc:
            raise CouldNotCheck("response is not valid JSON") from exc
    return response


def parse_model_catalog(response: Any) -> Tuple[str, ...]:
    """Extract model ids from a captured provider response.

    OpenAI- and Anthropic-shaped responses use ``data``; other compatible
    providers use ``models`` or ``items``.  A direct list is accepted for
    small fixtures.  Every accepted response must contain at least one model
    id, so an empty or changed response cannot be mistaken for "unchanged".
    """
    payload = _json_payload(response)
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, Mapping):
        entries = None
        for key in CATALOG_KEYS:
            if key in payload:
                entries = payload[key]
                break
        if entries is None:
            raise CouldNotCheck("response has no model catalogue")
    else:
        raise CouldNotCheck("response is not an object or list")

    if not isinstance(entries, list):
        raise CouldNotCheck("model catalogue is not a list")

    model_ids: List[str] = []
    for entry in entries:
        if isinstance(entry, str):
            model_id = entry
        elif isinstance(entry, Mapping):
            model_id = next(
                (entry.get(key) for key in MODEL_ID_KEYS
                 if isinstance(entry.get(key), str)),
                None,
            )
        else:
            model_id = None
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())

    if not model_ids:
        raise CouldNotCheck("model catalogue contains no model ids")
    return tuple(sorted(set(model_ids)))


# This handles both dotted versions (gpt-5.6) and the hyphenated version
# portions used by names such as claude-3-5-sonnet.  Numeric date suffixes are
# deliberately not treated as a different release family unless the vendor
# also exposes them as part of the model's version portion.
VERSION_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<version>\d+(?:\.\d+)*(?:-\d+)*)"
    r"(?P<suffix>(?:-[A-Za-z][A-Za-z0-9_.-]*)?)"
)


def _version_parts(model_id: str) -> Optional[Tuple[str, str, Tuple[int, ...]]]:
    match = VERSION_RE.search(model_id)
    if not match:
        return None
    try:
        version = tuple(int(part) for part in re.split(r"[.-]", match.group("version")))
    except ValueError:
        return None
    prefix = model_id[:match.start()].rstrip("-_.:/").lower()
    suffix = match.group("suffix").lstrip("-_.:/").lower()
    return prefix, suffix, version


def newer_versions(current_model: str, published_models: Iterable[str]) -> Tuple[str, ...]:
    """Return published versions newer than ``current_model``.

    Only the same model family is compared.  For example, a newer Sonnet
    release does not create a release notification for an Opus model, and a
    different `gpt` capability suffix does not become an accidental upgrade.
    """
    current = _version_parts(current_model)
    if current is None:
        raise CouldNotCheck(
            "current model {!r} has no comparable version".format(current_model)
        )
    prefix, suffix, version = current
    found = []
    for model_id in published_models:
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        candidate = _version_parts(model_id)
        if candidate is None:
            continue
        candidate_prefix, candidate_suffix, candidate_version = candidate
        if (candidate_prefix, candidate_suffix) == (prefix, suffix) and candidate_version > version:
            found.append((candidate_version, model_id))
    return tuple(model_id for _, model_id in sorted(set(found)))


def models_in_use(
    *,
    sources: Optional[Mapping[str, str]] = None,
    detector: Optional[Callable[[str], Mapping[str, Any]]] = None,
    providers: Optional[Mapping[str, str]] = None,
) -> Dict[str, Tuple[str, ...]]:
    """Group the model ids actually observed for each provider.

    ``heartbeat.detect_model`` remains the source of truth for model identity;
    this helper only joins it to the provider registry.  Agents with no
    observed session model are not treated as using a guessed default.
    """
    sources = heartbeat.MODEL_SOURCES if sources is None else sources
    detector = heartbeat.detect_model if detector is None else detector
    providers = heartbeat.PROVIDERS if providers is None else providers

    found: Dict[str, set] = {}
    for agent in sorted(sources):
        provider = providers.get(agent)
        if not provider:
            raise CouldNotCheck("agent {!r} has no provider".format(agent))
        detected = detector(agent)
        model_id = detected.get("model") if isinstance(detected, Mapping) else None
        if model_id is None:
            continue
        if not isinstance(model_id, str) or not model_id.strip():
            raise CouldNotCheck("agent {!r} reported an invalid model".format(agent))
        found.setdefault(provider, set()).add(model_id.strip())
    return {
        provider: tuple(sorted(model_ids))
        for provider, model_ids in sorted(found.items())
    }


def watch(
    current_models: Mapping[str, Iterable[str]],
    responses: Mapping[str, Any],
    notify: Optional[Callable[[TickTickTask], None]] = None,
) -> WatchResult:
    """Compare current models with captured catalogues and notify on changes.

    ``notify`` is a narrow side-effect boundary for the nightly job.  Tests
    can collect the emitted ``TickTickTask`` values without network access,
    while production wiring can send each payload to TickTick.  A missing or
    unparseable provider response becomes a ``could-not-check`` fault.
    """
    notifications: List[TickTickTask] = []
    faults: List[WatchFault] = []

    for provider in sorted(current_models):
        models = tuple(sorted(set(current_models[provider])))
        if provider not in responses:
            faults.append(WatchFault(provider, "no model catalogue response"))
            continue
        try:
            published = parse_model_catalog(responses[provider])
        except CouldNotCheck as exc:
            faults.append(WatchFault(provider, str(exc)))
            continue

        for current_model in models:
            try:
                releases = newer_versions(current_model, published)
            except CouldNotCheck as exc:
                faults.append(WatchFault(provider, str(exc)))
                continue
            for newer_model in releases:
                task = TickTickTask(provider, current_model, newer_model)
                notifications.append(task)
                if notify is not None:
                    notify(task)

    return WatchResult(tuple(notifications), tuple(faults))


def watch_in_use(
    responses: Mapping[str, Any],
    notify: Optional[Callable[[TickTickTask], None]] = None,
    *,
    sources: Optional[Mapping[str, str]] = None,
    detector: Optional[Callable[[str], Mapping[str, Any]]] = None,
    providers: Optional[Mapping[str, str]] = None,
) -> WatchResult:
    """Run :func:`watch` for the models observed in ``heartbeat`` sources."""
    return watch(
        models_in_use(sources=sources, detector=detector, providers=providers),
        responses,
        notify,
    )


__all__ = [
    "CouldNotCheck",
    "TickTickTask",
    "WatchFault",
    "WatchResult",
    "models_in_use",
    "newer_versions",
    "parse_model_catalog",
    "watch",
    "watch_in_use",
]
