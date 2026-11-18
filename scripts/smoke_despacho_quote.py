"""Smoke test de la cotización real de Despacho (SPEC-12).

Necesita un token de servicio de G7 con `aud=api-despacho` y
`scope=cotizaciones:calcular`. Sin token solo se puede comprobar el 401.

Uso (PowerShell):
  $env:DESPACHO_TOKEN = "..."; python scripts/smoke_despacho_quote.py --distrito Miraflores --sku SKU-1 --cantidad 1
  python scripts/smoke_despacho_quote.py --distrito Miraflores   # solo cobertura

Si se omiten las líneas, Despacho consulta únicamente cobertura y tarifa del
distrito; para costo y plazo completos el SKU debe existir y estar activo en
Productos.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

DEFAULT_BASE_URL = "https://taller-api-gateway.onrender.com/api/v1"


def build_payload(args: argparse.Namespace) -> dict:
    payload: dict = {
        "destino": {
            "destinatario": args.destinatario,
            "direccion": args.direccion,
            "distrito": args.distrito,
            "departamento": args.departamento,
            "provincia": args.provincia,
        }
    }
    if args.referencia:
        payload["destino"]["referencia"] = args.referencia
    if args.sku:
        payload["lineas"] = [
            {"sku": sku, "cantidad": args.cantidad} for sku in args.sku
        ]
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cotización real de Despacho")
    parser.add_argument("--token", default=os.getenv("DESPACHO_TOKEN", ""))
    parser.add_argument(
        "--base-url",
        default=os.getenv("DISPATCH_BASE_URL", DEFAULT_BASE_URL),
    )
    parser.add_argument("--distrito", default="Miraflores")
    parser.add_argument("--direccion", default="Av. Larco 1234, dpto. 502")
    parser.add_argument("--destinatario", default="Cliente Demo")
    parser.add_argument("--departamento", default="Lima")
    parser.add_argument("--provincia", default="Lima")
    parser.add_argument("--referencia", default="Frente al parque")
    parser.add_argument(
        "--sku",
        action="append",
        default=[],
        help="SKU real y activo en Productos (repetible). Sin SKU: solo cobertura.",
    )
    parser.add_argument("--cantidad", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=40.0)
    args = parser.parse_args(argv)

    if not args.token:
        print(
            "Falta el token: usa --token o la variable DESPACHO_TOKEN",
            file=sys.stderr,
        )
        return 2

    url = args.base_url.rstrip("/") + "/cotizaciones"
    payload = build_payload(args)
    print(f"POST {url}")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    response = httpx.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {args.token}"},
        timeout=args.timeout,
    )
    print(f"\nHTTP {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(response.text[:2000])
    return 0 if response.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
