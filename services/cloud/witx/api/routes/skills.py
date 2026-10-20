"""The skills the agent can load, and the person's own, written here."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends
from witx_contracts import api

from witx.api.deps import Deps
from witx.domain.rules import Rejected
from witx.harness import skills
from witx.harness.plugins import LOCAL, MINE, Installed, Offer
from witx.harness.skills import Skill
from witx.services.identity import Actor


def routes(d: Deps, plugins: Installed) -> APIRouter:
    router = APIRouter()
    CurrentActor = Annotated[Actor, Depends(d.actor)]

    def owner(who: Actor) -> None:
        # Skills are the deployment's: every task of every person may load them.
        if who.role != "owner":
            raise Rejected("owner_required", "only a tenant owner can change skills")

    def view(skill: Skill, marketplace: str) -> api.SkillView:
        return api.SkillView(
            name=skill.name,
            plugin=skill.plugin,
            marketplace=marketplace,
            version=skill.version,
            description=skill.description,
            apps=list(skill.apps),
            body=skill.body,
            editable=skill.plugin == MINE and marketplace == LOCAL,
            invoke="user" if skill.invoke == "user" else "auto",
        )

    @router.get("/api/v1/skills")
    async def list_skills(who: CurrentActor) -> api.SkillsView:
        """Every installed plugin's skills, and the plugins that are not loaded and why."""
        locked = await asyncio.to_thread(plugins.locked)
        found: list[api.SkillView] = []
        trouble: list[api.PluginTrouble] = []
        for name, plugin in (await asyncio.to_thread(plugins.load)).items():
            if isinstance(plugin, str):
                trouble.append(api.PluginTrouble(plugin=name, reason=plugin))
            else:
                found += [view(s, locked[name].marketplace) for s in plugin.skills]
        return api.SkillsView(skills=found, not_loaded=trouble)

    @router.put(f"/api/v1/skills/{MINE}/{{name}}")
    async def save_skill(name: str, body: api.SkillSave, who: CurrentActor) -> api.SkillView:
        """Saves one of the person's skills, new or changed; the agent can load it at once.
        Saving what is already saved changes nothing."""
        owner(who)
        try:
            skill = await asyncio.to_thread(
                plugins.save, name, body.description, body.apps, body.body, body.invoke
            )
        except ValueError as e:
            raise Rejected("invalid_skill", str(e)) from e
        return view(skill, LOCAL)

    @router.delete(f"/api/v1/skills/{MINE}/{{name}}", status_code=204)
    async def delete_skill(name: str, who: CurrentActor, purge: bool = False) -> None:
        """Deletes one of the person's skills; with ``purge``, its versions too."""
        owner(who)
        try:
            await asyncio.to_thread(plugins.delete, name, purge)
        except ValueError as e:
            raise Rejected("not_found", str(e)) from e

    @router.get(f"/api/v1/skills/{MINE}/{{name}}/history")
    async def skill_history(name: str, who: CurrentActor) -> list[api.SkillVersionView]:
        """The saved versions of one of the person's skills, newest first."""
        return [
            api.SkillVersionView(
                commit=v.commit,
                version=v.skill.version,
                saved_at=v.saved_at,
                description=v.skill.description,
                apps=list(v.skill.apps),
                body=v.skill.body,
                invoke="user" if v.skill.invoke == "user" else "auto",
            )
            for v in await asyncio.to_thread(plugins.history, name)
        ]

    @router.get("/api/v1/marketplaces")
    async def list_marketplaces(who: CurrentActor) -> api.MarketplacesView:
        """Where plugins come from, and the plugins installed from them; reads no network."""
        found = await asyncio.to_thread(plugins.marketplaces)
        locked = await asyncio.to_thread(plugins.locked)
        return api.MarketplacesView(
            marketplaces=[
                api.MarketplaceView(name=n, source=s) for n, s in found.items() if n != LOCAL
            ],
            installed=[
                api.InstalledPluginView(
                    name=e.name, marketplace=e.marketplace, version=e.version, commit=e.commit
                )
                for e in locked.values()
                if e.name != MINE
            ],
        )

    @router.get("/api/v1/marketplaces/{marketplace}/plugins")
    async def list_offers(marketplace: str, who: CurrentActor) -> list[api.PluginOfferView]:
        """What the marketplace offers now, each with its text and, for one installed,
        the installed text: fetched from its source, so it may take a while."""
        owner(who)
        try:
            offers = await asyncio.to_thread(plugins.offers, marketplace)
        except ValueError as e:
            raise Rejected("marketplace_unavailable", str(e)) from e
        return [await asyncio.to_thread(offer_view, o) for o in offers]

    def offer_view(offer: Offer) -> api.PluginOfferView:
        installed: dict[str, str] = {}
        if offer.name in plugins.locked():
            folder = plugins.folder(offer.name)
            installed = {f: (folder / f).read_text("utf-8") for f in skills.files(folder)}
        locked, plugin = offer.locked, offer.plugin
        return api.PluginOfferView(
            name=offer.name,
            version=plugin.version if plugin else "",
            description=plugin.description if plugin else "",
            skills=[s.name for s in plugin.skills] if plugin else [],
            source=locked.source if locked else "",
            commit=locked.commit if locked else "",
            sha256=locked.sha256 if locked else "",
            trouble=offer.trouble,
            files=[
                api.PluginFileView(path=f, text=t, installed=installed.get(f))
                for f, t in offer.files.items()
            ],
            removed=sorted(set(installed) - set(offer.files)),
        )

    @router.post("/api/v1/marketplaces/{marketplace}/plugins/{name}/install")
    async def install_plugin(
        marketplace: str, name: str, body: api.PluginInstall, who: CurrentActor
    ) -> api.InstalledPluginView:
        """Installs, or updates to, exactly what the person read: refused when the
        marketplace now offers something else."""
        owner(who)
        try:
            e = await asyncio.to_thread(
                plugins.install_seen, name, marketplace, body.commit, body.sha256
            )
        except ValueError as error:
            raise Rejected("marketplace_unavailable", str(error)) from error
        return api.InstalledPluginView(
            name=e.name, marketplace=e.marketplace, version=e.version, commit=e.commit
        )

    @router.delete("/api/v1/plugins/{name}", status_code=204)
    async def remove_plugin(name: str, who: CurrentActor) -> None:
        owner(who)
        try:
            await asyncio.to_thread(plugins.uninstall, name)
        except ValueError as e:
            raise Rejected("not_found", str(e)) from e

    return router
