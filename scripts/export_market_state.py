import asyncio
import json
import os
import pathlib
import re
import subprocess
from datetime import datetime, timezone
from decimal import Decimal

import certifi

os.environ.setdefault("GRPC_DNS_RESOLVER", "native")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CA_BUNDLE = ROOT / "tbank_ca_bundle.pem"
OUT = ROOT / "market_state.json"
TARGET = "invest-public-api.tbank.ru"
HOST = "invest-public-api.tbank.ru"

INSTRUMENTS = {
    "lukoil_zo31": {"isin": "RU000A1059R0", "label": "ЛУКОЙЛ ЗО-31", "preferred_class_codes": ["TQCB"]},
    "ofz_26247": {"isin": "RU000A108EF8", "label": "ОФЗ 26247", "preferred_class_codes": ["TQOB"]},
}


def quotation_to_decimal(value) -> Decimal:
    return Decimal(value.units) + Decimal(value.nano) / Decimal("1000000000")


def quote_level(level):
    return {
        "price": float(quotation_to_decimal(level.price)),
        "quantity": int(level.quantity),
    }


def choose_tradable_instrument(instruments, preferred_class_codes):
    for instrument in instruments:
        if (
            getattr(instrument, "api_trade_available_flag", False)
            and getattr(instrument, "class_code", "") in preferred_class_codes
        ):
            return instrument
    for instrument in instruments:
        if getattr(instrument, "api_trade_available_flag", False):
            return instrument
    return instruments[0]


def instrument_snapshot(instrument):
    return {
        "name": instrument.name,
        "ticker": instrument.ticker,
        "figi": instrument.figi,
        "uid": instrument.uid,
        "class_code": getattr(instrument, "class_code", None),
        "api_trade_available_flag": getattr(instrument, "api_trade_available_flag", None),
    }


def ensure_ca_bundle() -> None:
    if CA_BUNDLE.exists():
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", str(CA_BUNDLE))
        return
    proc = subprocess.run(
        ["openssl", "s_client", "-showcerts", "-connect", f"{HOST}:443", "-servername", HOST],
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    certs = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        proc.stdout,
        flags=re.S,
    )
    if len(certs) >= 2:
        CA_BUNDLE.write_text(
            pathlib.Path(certifi.where()).read_text(encoding="utf-8").rstrip()
            + "\n\n"
            + "\n\n".join(certs[1:])
            + "\n",
            encoding="utf-8",
        )
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", str(CA_BUNDLE))
    else:
        os.environ.setdefault("GRPC_DEFAULT_SSL_ROOTS_FILE_PATH", certifi.where())


async def main() -> None:
    token = os.environ.get("T_INVEST_TOKEN") or os.environ.get("INVEST_TOKEN")
    if not token:
        raise SystemExit("T_INVEST_TOKEN secret is not set")

    ensure_ca_bundle()

    from t_tech.invest import AsyncClient

    result = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "t-invest-api",
        "status": "ok",
        "instruments": {},
    }

    async with AsyncClient(token, target=TARGET) as client:
        for key, meta in INSTRUMENTS.items():
            found = await client.instruments.find_instrument(query=meta["isin"])
            if not found.instruments:
                result["instruments"][key] = {**meta, "status": "not_found"}
                continue

            instrument = choose_tradable_instrument(
                found.instruments,
                meta.get("preferred_class_codes", []),
            )
            state = {
                **meta,
                "status": "ok",
                **instrument_snapshot(instrument),
                "candidates": [instrument_snapshot(item) for item in found.instruments],
            }

            try:
                order_book = await client.market_data.get_order_book(
                    instrument_id=instrument.uid,
                    depth=10,
                )
                bids = [quote_level(level) for level in order_book.bids]
                asks = [quote_level(level) for level in order_book.asks]
                state["bids"] = bids
                state["asks"] = asks
                state["best_bid"] = bids[0] if bids else None
                state["best_ask"] = asks[0] if asks else None
                if bids and asks and bids[0]["price"]:
                    state["spread"] = round(asks[0]["price"] - bids[0]["price"], 6)
                    state["spread_pct"] = round((asks[0]["price"] - bids[0]["price"]) / bids[0]["price"] * 100, 4)
                else:
                    state["spread"] = None
                    state["spread_pct"] = None
            except Exception as exc:
                state["status"] = "orderbook_error"
                state["error"] = str(exc)

            result["instruments"][key] = state

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
