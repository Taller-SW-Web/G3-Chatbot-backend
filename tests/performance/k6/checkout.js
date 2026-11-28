// H5-P2 · Checkout (mod-cart + mod-checkout): varios clientes compran en paralelo durante
// DURATION (SLO doc §2.3 y §2.4). Cada VU usa un cliente ACTIVO distinto del fake de
// Seguridad, porque el carrito es uno por cliente: dos VUs con el mismo token pelearían
// por el mismo carrito.
import { check } from "k6";
import http from "k6/http";
import { Counter } from "k6/metrics";

import {
  API,
  DEMO_EMAIL,
  DEMO_PASSWORD,
  DURATION,
  SLO_ERROR_RATE,
  authHeaders,
  buildSummary,
  latencyThresholds,
  login,
  uuid,
} from "./lib/common.js";

const CHECKOUT_VUS = Number(__ENV.CHECKOUT_VUS || 2);
const MIN_PURCHASES_PER_SECOND = Number(__ENV.CHECKOUT_MIN_RATE || 0.3);
const SKUS = ["NIK-REV7-42-NEG", "NIK-REV7-42-BLA", "NIK-REV7-43-NEG"];
// One ACTIVE customer per VU, seeded in src/shared/infrastructure/fakes/seeds/security.json.
const CUSTOMERS = [
  { email: DEMO_EMAIL, password: DEMO_PASSWORD },
  { email: "perf+2@ejemplo.pe", password: DEMO_PASSWORD },
  { email: "perf+3@ejemplo.pe", password: DEMO_PASSWORD },
];
if (!Number.isInteger(CHECKOUT_VUS) || CHECKOUT_VUS < 1 || CHECKOUT_VUS > CUSTOMERS.length) {
  throw new Error(
    `CHECKOUT_VUS must be an integer between 1 and ${CUSTOMERS.length} `
      + `(one ACTIVE fake customer with its own cart per VU), got ${CHECKOUT_VUS}`,
  );
}

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
    checkout: { executor: "constant-vus", vus: CHECKOUT_VUS, duration: DURATION },
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

function addItem(headers, sku, tags) {
  return http.post(`${API}/carrito/items`, JSON.stringify({ sku, cantidad: 1 }), { headers, tags });
}

function cartSkus(response) {
  if (response.status !== 200) return [];
  const lines = response.json("lineas");
  return Array.isArray(lines) ? lines.map((line) => line.sku) : [];
}

// The two first customers add different SKUs: each cart must only contain its own line.
function checkCartIsolation(carts) {
  if (carts.length < 2) return;
  const [first, second] = carts;
  const tags = { endpoint: "aislamiento" };
  const firstAdd = addItem(first, SKUS[0], tags);
  const secondAdd = addItem(second, SKUS[1], tags);
  check(firstAdd, { "aislamiento: cliente 1 agrega": (r) => r.status < 300 });
  check(secondAdd, { "aislamiento: cliente 2 agrega": (r) => r.status < 300 });

  const firstCart = http.get(`${API}/carrito`, { headers: first, tags });
  const secondCart = http.get(`${API}/carrito`, { headers: second, tags });
  check(firstCart, { "aislamiento: carrito 1 solo ve SKU-1": (r) => cartSkus(r).join() === SKUS[0] });
  check(secondCart, { "aislamiento: carrito 2 solo ve SKU-2": (r) => cartSkus(r).join() === SKUS[1] });
}

export function setup() {
  const sessions = CUSTOMERS.slice(0, CHECKOUT_VUS).map((customer) => {
    const token = login(customer.email, customer.password);
    return { token, headers: authHeaders(token) };
  });
  const carts = sessions.map((session) => session.headers);
  carts.forEach(clearCart);
  checkCartIsolation(carts);
  carts.forEach(clearCart);
  return { tokens: sessions.map((session) => session.token) };
}

export default function ({ tokens }) {
  const token = tokens[(__VU - 1) % tokens.length];
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
