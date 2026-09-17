"""Who you are signed in as, and what you're allowed to do in this app.

Each PubPulse app resolves roles its own way — PricePulse keys off a bare
pub_id session, TaskPulse has is_task_admin, RotaPulse has app_admin /
rota_admin / staff, DiaryPulse has manager / staff — so until now the only
way to tell which one you held was to notice which nav links happened to
appear. This maps whatever THIS app uses onto the family's four roles, so the
chip in the nav (templates/_whoami.html) says the same four things in all
five apps:

    Owner    the account that pays — everything, including billing
    Manager  full use of the app, but no billing
    Staff    day-to-day use only
    Support  PubPulse's own read-only troubleshooting session

ROLES and the keys current_identity() returns are a shared contract: keep
them in sync across PricePulse, TaskPulse, RotaPulse, DiaryPulse and the Hub.
Only the resolution below is app-specific.
"""

from flask import g, session, url_for

from app import config

ROLES = {
    "owner": ("Owner", "Full access here, plus billing and inviting people."),
    "manager": ("Manager", "Full use of this app, but not billing or the subscription."),
    "staff": ("Staff", "Day-to-day use only — no settings, people or billing."),
    "support": ("Support", "PubPulse support, read-only. Nothing you change here will save."),
}


def _identity(role, *, name, email=None, place=None, logout_url,
              logout_post=False, manage_url=None):
    """One identity dict, in the shape templates/_whoami.html expects."""
    label, blurb = ROLES[role]
    return {
        "role": role,
        "role_label": label,
        "role_blurb": blurb,
        # Three of the apps take the name straight off the shared session, so
        # rather than render a nameless chip, fall back to the email and then
        # to something honest.
        "name": name or email or "Signed in",
        "email": email,
        "place": place,
        "logout_url": logout_url,
        "logout_post": logout_post,
        "manage_url": manage_url,
    }


# RotaPulse's own permission levels, highest first. app_admin is the owner
# (it gates venue configuration, owner-only in V1); rota_admin is what a
# Hub-invited manager is granted; staff is everyone else.
_LEVEL_ROLES = (("app_admin", "owner"), ("rota_admin", "manager"), ("staff", "staff"))


def current_identity():
    """The signed-in identity for the nav chip, or None if nobody is."""
    levels = g.get("permission_levels") or set()
    venue = g.get("venue")
    place = venue["name"] if venue else None

    if "support_readonly" in levels:
        return _identity("support", name="PubPulse", place=place,
                         logout_url=config.PRICEPULSE_ADMIN_LOGOUT_URL)

    person = g.get("person")
    if person is None:
        return None

    # Someone can hold more than one grant, so take the highest rather than
    # an arbitrary member of the set.
    role = next((r for level, r in _LEVEL_ROLES if level in levels), None)
    if role is None:
        return None

    if person["pub_id"] is None:
        # Invited through RotaPulse's own staff login rather than the family
        # cookie, so logging out means clearing that local session key — which
        # app/rota_login.py only does on a POST.
        try:
            logout_url = url_for("rota_login.logout")
        except Exception:
            # Off a slug-scoped page there's no venue to build the URL from.
            return None
        return _identity(role, name=person["name"], email=person["email"],
                         place=place, logout_url=logout_url, logout_post=True)

    # The owner person, authenticated by the shared PubPulse cookie. Only the
    # Hub's /logout clears that, which is why the old nav — which offered the
    # local POST log-out and nothing else — left an owner with no way out.
    return _identity(
        role, name=person["name"], email=person["email"], place=place,
        logout_url=f"{config.PUBPULSE_HUB_URL}/logout",
        manage_url=f"{config.PUBPULSE_HUB_URL}/people" if role == "owner" else None,
    )
