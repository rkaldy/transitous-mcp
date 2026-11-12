from datetime import datetime

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from server import Destination, Settings, Transitous, create_app


@pytest.fixture
def settings():
    return Settings(_env_file=None, transitous_contact="test@example.org")


def journey(walk=300, end="2026-11-13T08:00:00Z"):
    return {
        "startTime": "2026-11-13T07:00:00Z",
        "endTime": end,
        "duration": 3600,
        "transfers": 0,
        "legs": [
            {
                "mode": "BUS",
                "startTime": "2026-11-13T07:00:00Z",
                "endTime": "2026-11-13T07:55:00Z",
                "duration": 3300,
                "from": {"name": "Veverská Bítýška, náměstí", "stopId": "cz:origin"},
                "to": {"name": "Brno"},
                "routeShortName": "303",
            },
            {
                "mode": "WALK",
                "startTime": "2026-11-13T07:55:00Z",
                "endTime": end,
                "duration": walk,
                "distance": 360,
                "from": {"name": "Brno"},
                "to": {"name": "Cíl"},
            },
        ],
    }


def mock_api(itineraries=None, stops=None):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/geocode"):
            return httpx.Response(
                200,
                json=stops
                if stops is not None
                else [
                    {
                        "type": "STOP",
                        "name": "Veverská Bítýška,náměstí",
                        "id": "cz:origin",
                    }
                ],
            )
        assert request.url.path == "/api/v6/plan"
        return httpx.Response(
            200,
            json={
                "itineraries": itineraries if itineraries is not None else [journey()]
            },
        )

    return httpx.MockTransport(handle), requests


async def test_plan_parameters_and_origin_cache(settings):
    settings.walk_speed_mps = 0.9
    settings.max_walk_seconds = 600
    transport, requests = mock_api()
    planner = Transitous(settings, transport)
    for _ in range(2):
        result = await planner.search(
            Destination(latitude=49.2, longitude=16.6),
            datetime.fromisoformat("2026-11-13T09:00:00+01:00"),
        )
    assert len(requests) == 3
    params = requests[-1].url.params
    assert params["fromPlace"] == "cz:origin"
    assert params["toPlace"] == "49.2,16.6"
    assert params["time"] == "2026-11-13T08:00:00+00:00"
    assert params["arriveBy"] == "true"
    assert params["timetableView"] == "false"
    assert params["maxPostTransitTime"] == "600"
    assert params["pedestrianSpeed"] == "0.9"
    assert params["directModes"] == ""
    assert "test@example.org" in requests[-1].headers["User-Agent"]
    assert result["itineraries"][0]["lines"][0]["line"] == "303"
    assert set(result) == {"itineraries"}


async def test_filters_late_arrivals_and_walking_transfers(settings):
    transfer = journey()
    transfer["legs"].insert(1, {**transfer["legs"][1], "duration": 700})
    transport, _ = mock_api(
        [journey(901), journey(end="2026-11-13T08:01:00Z"), transfer, journey(900)]
    )
    result = await Transitous(settings, transport).search(
        Destination(latitude=49, longitude=16),
        datetime.fromisoformat("2026-11-13T08:00:00Z"),
    )
    assert len(result["itineraries"]) == 1


async def test_no_connections(settings):
    transport, _ = mock_api([])
    result = await Transitous(settings, transport).search(
        Destination(latitude=49, longitude=16),
        datetime.fromisoformat("2026-11-13T08:00:00Z"),
    )
    assert result == {"itineraries": []}


@pytest.mark.parametrize(
    "stops",
    [
        [],
        [
            {"type": "STOP", "name": "Veverská Bítýška, náměstí", "id": "a"},
            {"type": "STOP", "name": "Veverská Bítýška náměstí", "id": "b"},
        ],
        [{"type": "STOP", "name": "Jiná zastávka", "id": "a"}],
    ],
)
async def test_never_silently_selects_wrong_stop(settings, stops):
    transport, requests = mock_api(stops=stops)
    with pytest.raises(ToolError, match="jednoznačně"):
        await Transitous(settings, transport).search(
            Destination(latitude=49, longitude=16),
            datetime.fromisoformat("2026-11-13T08:00:00Z"),
        )
    assert len(requests) == 1


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "schema"])
async def test_upstream_errors_are_not_empty_results(settings, failure):
    def handle(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "http":
            return httpx.Response(429)
        if failure == "json":
            return httpx.Response(200, text="invalid")
        return httpx.Response(200, json={"unexpected": []})

    with pytest.raises(ToolError):
        await Transitous(settings, httpx.MockTransport(handle)).search(
            Destination(latitude=49, longitude=16),
            datetime.fromisoformat("2026-11-13T08:00:00Z"),
        )


@pytest.mark.parametrize(
    "values",
    [{"max_walk_seconds": -1}, {"walk_speed_mps": 0}, {"walk_speed_mps": float("nan")}],
)
def test_invalid_settings(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, transitous_contact="test@example.org", **values)


async def rpc(client, method, params=None):
    response = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


@pytest.mark.parametrize("has_connections", [True, False])
async def test_mcp_http_initialize_list_and_call(settings, has_connections):
    transport, requests = mock_api(None if has_connections else [])
    app = create_app(settings, transport)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8000"
        ) as client,
    ):
        initialized = await rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert "tools" in initialized["capabilities"]
        tools = (await rpc(client, "tools/list"))["tools"]
        assert [t["name"] for t in tools] == ["find_connection"]
        assert set(tools[0]["inputSchema"]["properties"]) == {
            "destination",
            "arrival_time",
        }
        args = {
            "destination": {"latitude": 49.2, "longitude": 16.6},
            "arrival_time": "2026-11-13T09:00:00+01:00",
        }
        result = await rpc(
            client, "tools/call", {"name": "find_connection", "arguments": args}
        )
        assert not result.get("isError")
        assert result["content"] == []
        assert set(result["structuredContent"]) == {"itineraries"}
        if has_connections:
            assert result["structuredContent"]["itineraries"][0]["final_walk"] == {
                "distance_meters": 360,
                "duration_seconds": 300,
            }
            assert (
                result["structuredContent"]["itineraries"][0]["lines"][0]["line"]
                == "303"
            )
        else:
            assert result["structuredContent"]["itineraries"] == []
        for invalid in [
            {**args, "arrival_time": "2026-11-13T09:00:00"},
            {**args, "destination": {"latitude": 91, "longitude": 16}},
            {**args, "arrival_time": "tomorrow"},
        ]:
            result = await rpc(
                client, "tools/call", {"name": "find_connection", "arguments": invalid}
            )
            assert result["isError"]
        assert len(requests) == 2
        assert (await client.get("/other")).status_code == 404


def test_env_configuration(monkeypatch):
    monkeypatch.setenv("TRANSITOUS_CONTACT", "test@example.org")
    monkeypatch.setenv("MAX_WALK_SECONDS", "420")
    monkeypatch.setenv("WALK_SPEED_MPS", "1.4")
    settings = Settings(_env_file=None)
    assert settings.max_walk_seconds == 420
    assert settings.walk_speed_mps == 1.4


@pytest.mark.parametrize("stay_seated", [False, True])
async def test_compact_transfers_include_walking_and_waiting(settings, stay_seated):
    trip = journey()
    first = trip["legs"][0]
    first["endTime"] = "2026-11-13T07:20:00Z"
    first["duration"] = 1200
    first["legGeometry"] = {"points": "must-not-leak"}
    transfer_walk = {
        **trip["legs"][1],
        "startTime": "2026-11-13T07:20:00Z",
        "endTime": "2026-11-13T07:25:00Z",
        "duration": 300,
        "to": {"name": "Brno, nástupiště 2"},
    }
    second = {
        **first,
        "routeShortName": "",
        "displayName": "Tram 1",
        "from": {"name": "Brno, nástupiště 2"},
        "to": {"name": "Centrum"},
        "startTime": "2026-11-13T07:30:00Z",
        "endTime": "2026-11-13T07:55:00Z",
        "duration": 1500,
        "interlineWithPreviousLeg": stay_seated,
    }
    trip["legs"][1:1] = ([] if stay_seated else [transfer_walk]) + [second]
    transport, _ = mock_api([trip])
    result = await Transitous(settings, transport).search(
        Destination(latitude=49, longitude=16),
        datetime.fromisoformat("2026-11-13T08:00:00Z"),
    )
    summary = result["itineraries"][0]
    assert set(summary) == {"departure", "arrival", "lines", "transfers", "final_walk"}
    assert summary["final_walk"] == {"distance_meters": 360, "duration_seconds": 300}
    assert summary["departure"] == "2026-11-13T07:00:00+00:00"
    assert summary["arrival"] == "2026-11-13T08:00:00+00:00"
    assert [line["line"] for line in summary["lines"]] == ["303", "Tram 1"]
    assert set(summary["lines"][0]) == {"line", "from", "to", "departure", "arrival"}
    assert summary["transfers"] == (
        []
        if stay_seated
        else [
            {
                "from_stop": "Brno",
                "to_stop": "Brno, nástupiště 2",
                "arrival": "2026-11-13T07:20:00+00:00",
                "departure": "2026-11-13T07:30:00+00:00",
                "duration_seconds": 600,
            }
        ]
    )


@pytest.mark.parametrize(
    ("distances", "expected_distance", "expected_duration"),
    [([], 0, 0), ([None], None, 300), ([120, 240], 360, 600), ([120, None], None, 600)],
)
async def test_final_walk_aggregation(
    settings, distances, expected_distance, expected_duration
):
    trip = journey()
    walk = trip["legs"].pop()
    for distance in distances:
        leg = {**walk}
        if distance is None:
            leg.pop("distance")
        else:
            leg["distance"] = distance
        trip["legs"].append(leg)
    transport, _ = mock_api([trip])
    result = await Transitous(settings, transport).search(
        Destination(latitude=49, longitude=16),
        datetime.fromisoformat("2026-11-13T08:00:00Z"),
    )
    assert result["itineraries"][0]["final_walk"] == {
        "distance_meters": expected_distance,
        "duration_seconds": expected_duration,
    }
