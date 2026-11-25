// H5-P2 · Checkout (mod-cart + mod-checkout): un cliente compra en bucle durante DURATION
// (SLO doc §2.3 y §2.4). Un solo VU: el fake de Seguridad tiene un único cliente activo y el
// carrito es uno por cliente, así que dos compras en paralelo pelearían por el mismo carrito.
import { check } from "k6";
import http from "k6/http";
import { Counter } from "k6/metrics";

import {
  API,
  DURATION,
  SLO_ERROR_RATE,
  authHeaders,
  buildSummary,
  latencyThresholds,
  login,
  uuid,
} from "./lib/common.js";

const MIN_PURCHASES_PER_SECOND = Number(__ENV.CHECKOUT_MIN_RATE || 0.3);
const SKUS = ["NIK-REV7-42-NEG", "NIK-REV7-42-BLA", "NIK-REV7-43-NEG"];
// SPEC-14 test cards: 4111... is always APPROVED.
const CARD = { numeroTarjeta: "4111111111111111", titular: "CLIENTE DEMO", vencimiento: "12/30", cvv: "123" };
const ADDRESS = {
  destinatario: "Cliente Demo",
  direccionExacta: "Av. Larco 1234",
  distrito: "Miraflores",
  provincia: "Lima",
  departamento: "Lima",
};
const CONTACT = {
  nombreCompleto: "Cliente Demo",
  tipoDocumento: "DNI",
  numeroDocumento: "12345678",
  telefono: "987654321",
  email: "cliente@demo.pe",
};

const purchases = new Counter("compras");

export const options = {
  scenarios: {
    checkout: { executor: "constant-vus", vus: 1, duration: DURATION },
  },
  thresholds: {
    ...latencyThresholds(["agregar_carrito", "ver_carrito", "crear_checkout", "pagar"]),
    http_req_failed: [`rate<${SLO_ERROR_RATE}`],
    checks: ["rate==1"],
    compras: [`rate>=${MIN_PURCHASES_PER_SECOND}`],
  },
};

function clearCart(headers) {
  http.del(`${API}/carrito`, JSON.stringify({ confirmado: true }), { headers, tags: { endpoint: "vaciar" } });
}

export function setup() {
  const token = login();
  clearCart(authHeaders(token));
  return { token };
}

export default function ({ token }) {
  const headers = authHeaders(token);
  const sku = SKUS[__ITER % SKUS.length];

  const add = http.post(`${API}/carrito/items`, JSON.stringify({ sku, cantidad: 1 }), {
    headers,
    tags: { endpoint: "agregar_carrito" },
  });
  const cart = http.get(`${API}/carrito`, { headers, tags: { endpoint: "ver_carrito" } });
  const created = http.post(`${API}/checkout`, JSON.stringify({ direccion: ADDRESS, contacto: CONTACT }), {
    headers: authHeaders(token, { "Idempotency-Key": uuid() }),
    tags: { endpoint: "crear_checkout" },
  });
  const ok = check(add, { "agregar 201/200": (r) => r.status < 300 })
    && check(cart, { "carrito con la línea": (r) => r.status === 200 && r.json("lineas").length > 0 })
    && check(created, { "checkout PENDING_PAYMENT": (r) => r.status === 201 && r.json("status") === "PENDING_PAYMENT" });
  if (!ok) {
    clearCart(headers);
    return;
  }

  const paid = http.post(`${API}/checkout/${created.json("id")}/pago`, JSON.stringify(CARD), {
    headers: authHeaders(token, { "Idempotency-Key": uuid() }),
    tags: { endpoint: "pagar" },
  });
  if (check(paid, { "pago APROBADO": (r) => r.status === 200 && r.json("resultado") === "APROBADO" })) {
    purchases.add(1);
  } else {
    clearCart(headers);
  }
}

export function handleSummary(data) {
  return buildSummary("checkout", data);
}
