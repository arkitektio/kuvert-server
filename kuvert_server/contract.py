"""What this image answers a hub's installer: ``python -m arkitekt_service <verb>`` (see ``arkitekt_service.contract``).

The installer knows the hub; how this release spells its config is written here, with the
settings it is read by. A key renamed in ``configuration.py`` is renamed in :func:`render` in
the same commit, and no installer has to learn of it.
"""

from __future__ import annotations

from arkitekt_service.contract import JSON, Contract, Description, Facts, Needs, Offers, Refused, Scope, blocks

from kuvert_server.configuration import Settings

#: What a token may be allowed to do here: defined at the coordination server when the hub enrols.
SCOPES = [
    Scope(key="kuvert_read", description="Read synced mail"),
    Scope(key="kuvert_write", description="Link mailboxes, organise and send mail"),
]


def render(facts: Facts) -> dict[str, JSON]:
    """This release's config for the hub ``facts`` describes."""
    document: dict[str, JSON] = blocks.server(facts)
    key = facts.secrets.get("fernet")
    if key is None:
        raise Refused("it needs a Fernet key file mounted: stored mailbox credentials are encrypted with it")
    document["secrets"] = {"key_path": key}
    if facts.storage is not None:
        document["datalayer"] = blocks.datalayer(facts)
    document["instance"] = blocks.instance(facts)
    hook = blocks.rekuest_hook(facts)
    if hook is not None:
        document["rekuest_hook"] = hook
    return document


contract = Contract(
    description=Description(
        name="kuvert",
        summary="Mail, synced and organised.",
        needs=Needs(scopes=SCOPES, storage=["bigfile"], instance_key=True, peers=["rekuest"], secrets=["fernet"]),
        offers=Offers(endpoints={"rekuest_service": "_rekuest/service", "rekuest_hook": "_rekuest/hook"}),
        requires={"rekuest": ">=6"},
    ),
    settings=Settings,
    render=render,
    setup=(("ensureadmin",),),
)
