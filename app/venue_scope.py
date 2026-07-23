"""
Shared per-blueprint machinery for the /v/<slug>/... URL space.

Turns a URL's <slug> segment into g.venue and pops it out of the view
function's own kwargs, so view functions don't need a slug parameter added
to every single one. A matching url_defaults hook threads the current slug
back into every url_for() call automatically, so templates and redirects
across the whole app need no changes either. Ported near-verbatim from
taskpulse/app/venue_scope.py — same idiom, own `venue` table.

register_venue_gate additionally enforces the "whole venue locks if the
subscription isn't active" rule (see app.billing.current_venue_plan) for
blueprints where that's wanted — deliberately NOT applied to the billing
blueprint itself, since starting/fixing a subscription must stay reachable
exactly when it's inactive.
"""

import flask

from app.db import get_db, get_venue_by_slug


def register_venue_scope(blueprint):
    @blueprint.url_value_preprocessor
    def _pull_venue(endpoint, values):
        if values is None or "slug" not in values:
            return
        slug = values.pop("slug")
        venue = get_venue_by_slug(get_db(), slug)
        if venue is None:
            flask.abort(404)
        flask.g.venue = venue
        flask.g.slug = slug

    @blueprint.url_defaults
    def _add_slug(endpoint, values):
        if "slug" not in values and flask.g.get("slug"):
            values["slug"] = flask.g.slug


def register_venue_gate(blueprint):
    """Adds the whole-venue subscription gate on top of register_venue_scope.
    Import is local to avoid a circular import (billing imports get_db/config
    only, never venue_scope)."""

    @blueprint.before_request
    def _require_active_venue():
        from app.billing import current_venue_plan

        venue = flask.g.get("venue")
        if venue is None:
            return  # a non-slug route on this blueprint, if any
        if current_venue_plan(venue["id"]) != "active":
            return flask.render_template("locked.html", venue=venue)
