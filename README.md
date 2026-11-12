# Transitous MCP

Jediný MCP nástroj `find_connection` vyhledává veřejnou dopravu ze zastávky
**Veverská Bítýška, náměstí** do souřadnic s příchodem **nejpozději v zadaný čas**.
Zahrnuje pěší cestu od poslední zastávky k cíli. Server používá Streamable HTTP
na jediné cestě `/mcp` a oficiální Python MCP SDK 2.x.

## Spuštění

Vyžaduje Python 3.13+ a [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env
# V .env vyplňte TRANSITOUS_CONTACT svým kontaktem.
uv sync
uv run python server.py
```

Lokální adresa je `http://127.0.0.1:8000/mcp`. OpenAI API klíč není potřeba.
Soubor `.env` se načítá z pracovního adresáře; skutečné proměnné prostředí mají přednost.

| Proměnná | Výchozí hodnota | Význam |
| --- | --- | --- |
| `TRANSITOUS_CONTACT` | povinná | E-mail nebo HTTPS URL provozovatele v User-Agent |
| `MAX_WALK_SECONDS` | `900` | Maximum sekund každého souvislého pěšího přesunu, včetně přestupů |
| `WALK_SPEED_MPS` | `1.2` | Rychlost chůze v metrech za sekundu (1,2 m/s = 4,32 km/h) |
| `HOST` | `127.0.0.1` | Adresa pro naslouchání; pro kontejner `0.0.0.0` |
| `PORT` | `8000` | HTTP port |
| `ALLOWED_HOSTS` | lokální hostitelé | JSON seznam povolených HTTP Host; pro nasazení přidejte veřejnou doménu |
| `ALLOWED_ORIGINS` | localhost a ChatGPT | JSON seznam povolených Origin |

Záporný limit, nulová či neplatná rychlost způsobí chybu při startu. Limit 0
zakazuje pěší přesuny s nenulovou dobou. Limit platí pro každý přesun samostatně,
nikoli součet chůze celé cesty. Transitous může vlastními limity omezit velké hodnoty.

## Volání

Argumenty nástroje `find_connection`:

```json
{
  "destination": {"latitude": 49.1951, "longitude": 16.6068},
  "arrival_time": "2026-11-13T09:00:00+01:00"
}
```

Čas musí obsahovat datum a UTC offset (nebo `Z`). Pro český místní čas použijte
offset odpovídající datu: zpravidla `+01:00` v zimě, `+02:00` v létě.
Pouhý čas bez data nebo časového pásma je odmítnut.

Výstup obsahuje pouze `itineraries` s celkovým odjezdem a příchodem,
použitými linkami, přestupy a závěrečnou chůzí. MCP jej vrací v `structuredContent`, bez textové
kopie (`content: []`), i když nebylo nalezeno spojení. Chybová hlášení zůstávají textová.
Celkový `arrival` zahrnuje i závěrečnou chůzi;
`arrival` u linky znamená příjezd vozidla. Časy jsou ISO 8601 s časovým pásmem.
Příklad jednoho přímého spojení:

```json
{
  "itineraries": [{
    "departure": "2026-11-13T07:00:00+00:00",
    "arrival": "2026-11-13T08:00:00+00:00",
    "final_walk": {"distance_meters": 360, "duration_seconds": 300},
    "lines": [{
      "line": "303",
      "from": "Veverská Bítýška, náměstí",
      "to": "Brno",
      "departure": "2026-11-13T07:00:00+00:00",
      "arrival": "2026-11-13T07:55:00+00:00"
    }],
    "transfers": []
  }]
}
```

Každý přestup obsahuje `from_stop`, `to_stop`, `arrival`, `departure` a
`duration_seconds` (celý interval mezi spoji, včetně chůze a čekání).
Pokračování ve stejném vozidle se nevykazuje jako přestup. Název linky se bere
z `routeShortName`, případně `displayName` nebo `routeLongName`; bez názvu je `null`.
`{"itineraries": []}` znamená, že nebylo vráceno vyhovující spojení;
nedostupné API a neplatné odpovědi jsou MCP chyby.

`final_walk` obsahuje vzdálenost v metrech a dobu v sekundách pro závěrečnou
chůzi od poslední zastávky do cíle. Navazující pěší úseky se sčítají; chůze
při předchozích přestupech se sem nezapočítává. Pokud API nevrátí vzdálenost,
`distance_meters` je `null`. Bez závěrečné chůze jsou obě hodnoty `0`.

ID výchozí zastávky se zjišťuje přes geocoding a uchovává hodinu. Shoda názvu
ignoruje diakritiku, mezery a interpunkci; nejednoznačný výsledek skončí chybou.
Plánování používá `arriveBy=true` a `timetableView=false`. Pěší limit se posílá
API pro přístup/docházku a kontroluje na vrácených úsecích, včetně přestupů.
Filtrování může odstranit všechny výsledky; server neprovádí další prohledávání,
takže prázdný seznam není důkazem, že v jízdních řádech žádné spojení neexistuje.

## Připojení do ChatGPT

Server připojte přes Secure MCP Tunnel nebo jej zpřístupněte přes HTTPS reverzní
proxy/tunel a v ChatGPT použijte adresu `https://vase-domena/mcp`.
Pro veřejnou doménu upravte `ALLOWED_HOSTS`, např.
`["localhost:*","127.0.0.1:*","spoje.example.cz"]`.
Postup připojení a dostupnost funkcí popisuje
[oficiální dokumentace OpenAI](https://developers.openai.com/plugins/deploy/connect-chatgpt).
Server sám neimplementuje přihlášení; pro osobní použití lze využít soukromý tunel.

## Ověření

```sh
uv run pytest
```

Testy používají simulované HTTP odpovědi. Ověřují MCP initialize/list/call,
validaci vstupů, parametry plánování, cache zastávky, limity chůze a chyby API.
Živý smoke test: spusťte server, připojte MCP klienta a zavolejte nástroj
s časem v aktuální platnosti jízdních řádů.

## Data a podmínky služby

[Transitous API](https://transitous.org/api/) vyžaduje identifikaci aplikace
a kontakt v User-Agent. Veřejná služba je určena nekomerčním open-source aplikacím;
před běžným provozem zveřejněte zdroj pod vhodnou open-source licencí.
Pro intenzivní provoz respektujte pravidla poskytovatele.

Zdroje: [Transitous a dopravní data](https://transitous.org/sources/),
[© OpenStreetMap contributors](https://www.openstreetmap.org/copyright).
Tyto odkazy jsou také uvedeny v instrukcích MCP serveru pro zobrazení zdrojů dat.
Implementace vychází z [MOTIS OpenAPI v2.10.2](https://raw.githubusercontent.com/motis-project/motis/refs/tags/v2.10.2/openapi.yaml).
