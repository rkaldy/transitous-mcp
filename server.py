"""Single-tool Streamable HTTP MCP server backed by Transitous."""

import asyncio
import re
import time
import unicodedata
from datetime import UTC
from itertools import pairwise, takewhile
from typing import Annotated, Any

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

ORIGIN = "Veverská Bítýška, náměstí"
ATTRIBUTION = "Transitous: https://transitous.org/sources/; © OpenStreetMap contributors: https://www.openstreetmap.org/copyright"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    transitous_contact: str = Field(min_length=3)
    max_walk_seconds: int = Field(default=900, ge=0)
    walk_speed_mps: float = Field(default=1.2, gt=0, allow_inf_nan=False)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    allowed_hosts: list[str] = ["localhost:*", "127.0.0.1:*", "[::1]:*"]
    allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "https://chatgpt.com",
    ]

    @field_validator("transitous_contact")
    @classmethod
    def validate_contact(cls, value: str) -> str:
        if not value.isascii() or any(c.isspace() for c in value):
            raise ValueError("Kontakt musí být ASCII e-mail nebo URL bez mezer.")
        if "@" not in value and not value.startswith("https://"):
            raise ValueError("Uveďte kontaktní e-mail nebo HTTPS URL.")
        return value


class Destination(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class Place(BaseModel):
    name: str
    stopId: str | None = None


class Leg(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")
    mode: str
    startTime: AwareDatetime
    endTime: AwareDatetime
    duration: int = Field(ge=0)
    distance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    from_place: Place = Field(alias="from")
    to: Place
    routeShortName: str | None = None
    displayName: str | None = None
    routeLongName: str | None = None
    interlineWithPreviousLeg: bool = False


class Itinerary(BaseModel):
    model_config = ConfigDict(extra="allow")
    startTime: AwareDatetime
    endTime: AwareDatetime
    duration: int = Field(ge=0)
    transfers: int = Field(ge=0)
    legs: list[Leg] = Field(min_length=1)


class Plan(BaseModel):
    itineraries: list[Itinerary]


class Stop(BaseModel):
    type: str
    name: str
    id: str = Field(min_length=1)


def normalized_name(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name.casefold())
    return re.sub(r"[^a-z0-9]", "", plain)


def summarize_itinerary(itinerary: Itinerary) -> dict[str, Any]:
    transit = [leg for leg in itinerary.legs if leg.mode != "WALK"]
    final_walk = list(
        takewhile(lambda leg: leg.mode == "WALK", reversed(itinerary.legs))
    )
    distances = [leg.distance for leg in final_walk]
    return {
        "departure": itinerary.startTime.isoformat(),
        "arrival": itinerary.endTime.isoformat(),
        "final_walk": {
            "distance_meters": (
                sum(distance for distance in distances if distance is not None)
                if all(distance is not None for distance in distances)
                else None
            ),
            "duration_seconds": sum(leg.duration for leg in final_walk),
        },
        "lines": [
            {
                "line": leg.routeShortName or leg.displayName or leg.routeLongName,
                "from": leg.from_place.name,
                "to": leg.to.name,
                "departure": leg.startTime.isoformat(),
                "arrival": leg.endTime.isoformat(),
            }
            for leg in transit
        ],
        "transfers": [
            {
                "from_stop": previous.to.name,
                "to_stop": following.from_place.name,
                "arrival": previous.endTime.isoformat(),
                "departure": following.startTime.isoformat(),
                "duration_seconds": int(
                    (following.startTime - previous.endTime).total_seconds()
                ),
            }
            for previous, following in pairwise(transit)
            if not following.interlineWithPreviousLeg
        ],
    }


class Transitous:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings
        self.transport = transport
        self._stop: Stop | None = None
        self._stop_expires = 0.0
        self._lock = asyncio.Lock()

    async def request(self, client: httpx.AsyncClient, path: str, params: dict) -> Any:
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException as exc:
            raise ToolError(
                "Transitous neodpověděl včas. Zkuste požadavek později."
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ToolError(
                f"Transitous vrátil HTTP {exc.response.status_code}."
            ) from exc
        except (httpx.RequestError, ValueError) as exc:
            raise ToolError(
                "Transitous je nedostupný nebo vrátil neplatnou odpověď."
            ) from exc

    async def origin(self, client: httpx.AsyncClient) -> Stop:
        async with self._lock:
            if self._stop is not None and time.monotonic() < self._stop_expires:
                return self._stop
            data = await self.request(
                client,
                "v1/geocode",
                {
                    "text": ORIGIN,
                    "type": "STOP",
                    "language": "cs",
                    "numResults": 10,
                },
            )
            try:
                if not isinstance(data, list):
                    raise TypeError("Expected stop list")
                stops = [Stop.model_validate(item) for item in data]
            except (TypeError, ValidationError) as exc:
                raise ToolError("Transitous vrátil neplatný seznam zastávek.") from exc
            matches = {
                s.id: s
                for s in stops
                if s.type == "STOP"
                and normalized_name(s.name) == normalized_name(ORIGIN)
            }
            if len(matches) != 1:
                raise ToolError(
                    "Výchozí zastávku Veverská Bítýška, náměstí nelze jednoznačně určit."
                )
            self._stop = next(iter(matches.values()))
            self._stop_expires = time.monotonic() + 3600
            return self._stop

    async def search(
        self, destination: Destination, arrival_time: AwareDatetime
    ) -> dict:
        async with httpx.AsyncClient(
            base_url="https://api.transitous.org/api/",
            headers={
                "User-Agent": f"transitous-mcp/0.1.0 ({self.settings.transitous_contact})"
            },
            timeout=30,
            transport=self.transport,
        ) as client:
            origin = await self.origin(client)
            data = await self.request(
                client,
                "v6/plan",
                {
                    "fromPlace": origin.id,
                    "toPlace": f"{destination.latitude},{destination.longitude}",
                    "time": arrival_time.astimezone(UTC).isoformat(),
                    "arriveBy": "true",
                    "timetableView": "false",
                    "transitModes": "TRANSIT",
                    "directModes": "",
                    "preTransitModes": "WALK",
                    "postTransitModes": "WALK",
                    "maxPreTransitTime": self.settings.max_walk_seconds,
                    "maxPostTransitTime": self.settings.max_walk_seconds,
                    "pedestrianSpeed": self.settings.walk_speed_mps,
                    "useRoutedTransfers": "true",
                    "detailedTransfers": "true",
                    "detailedLegs": "false",
                    "language": "cs",
                },
            )
        try:
            plan = Plan.model_validate(data)
        except ValidationError as exc:
            raise ToolError("Transitous vrátil neplatný plán cesty.") from exc
        accepted = []
        for itinerary in plan.itineraries:
            if itinerary.endTime > arrival_time:
                continue
            # Sum consecutive walking legs so splitting a transfer cannot bypass the cap.
            walking = 0
            valid = True
            for leg in itinerary.legs:
                walking = walking + leg.duration if leg.mode == "WALK" else 0
                if walking > self.settings.max_walk_seconds:
                    valid = False
                    break
            if valid and any(leg.mode != "WALK" for leg in itinerary.legs):
                accepted.append(itinerary)
        accepted.sort(key=lambda i: i.startTime, reverse=True)
        return {
            "itineraries": [summarize_itinerary(i) for i in accepted],
        }


def create_server(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> MCPServer:
    planner = Transitous(settings, transport)
    mcp = MCPServer(
        "Transitous – Veverská Bítýška",
        version="0.1.0",
        instructions=f"Ve výsledné odpovědi uveď zdroje dat: {ATTRIBUTION}",
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    async def find_connection(
        destination: Destination,
        arrival_time: Annotated[
            AwareDatetime,
            Field(
                description="Nejpozdější příjezd: ISO 8601 datum a čas s UTC offsetem, např. 2026-11-13T09:00:00+01:00."
            ),
        ],
    ) -> CallToolResult:
        """Najde veřejnou dopravu ze zastávky Veverská Bítýška, náměstí do cílových
        souřadnic s příchodem nejpozději v arrival_time, včetně závěrečné chůze.
        Pro český místní čas použij správný offset Europe/Prague pro dané datum.
        Limity chůze určuje server. Výsledek obsahuje linky, přestupy a jejich časy.
        Celkový arrival zahrnuje závěrečnou chůzi; doba přestupu zahrnuje chůzi i čekání.
        final_walk uvádí délku v metrech a dobu v sekundách od poslední zastávky do cíle.
        Neznámá vzdálenost je null.
        Prázdný seznam znamená, že API nevrátilo vyhovující spojení, nikoli výpadek.
        """
        return CallToolResult(
            content=[],
            structuredContent=await planner.search(destination, arrival_time),
        )

    return mcp


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
):
    settings = settings or Settings()
    return create_server(settings, transport).streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        ),
    )


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
