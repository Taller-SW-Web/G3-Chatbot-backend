// H5-P2 · Catálogo (mod-catalog): 20 solicitudes/s durante DURATION (SLO doc §2.1 y §2.4).
// Mix: 60 % búsqueda, 20 % detalle, 10 % promociones, 10 % disponibilidad.
import { check } from "k6";
import http from "k6/http";

import { API, DURATION, SLO_ERROR_RATE, buildSummary, latencyThresholds } from "./lib/common.js";

const RATE = Number(__ENV.CATALOG_RATE || 20);
const QUERIES = ["zapatillas", "zapatillas running", "polo", "mochila", "short", "casaca", "nike", "adidas"];
// Seed products of the Productos fake that have a validated price: the others answer 503 by
// design (the fake mirrors G6 products without a channel price), which is not what we measure.
const PRODUCT_IDS = (__ENV.CATALOG_PRODUCT_IDS || "prod-001,prod-004,prod-005,prod-007,prod-009,prod-011,prod-014,prod-017")
  .split(",");
const SKUS = ["NIK-REV7-42-NEG", "NIK-REV7-42-BLA", "NIK-REV7-43-NEG"];

export const options = {
  scenarios: {
    catalogo: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 20,
      maxVUs: 60,
    },
  },
  thresholds: {
    ...latencyThresholds(["buscar", "detalle", "promociones", "disponibilidad"]),
    http_req_failed: [`rate<${SLO_ERROR_RATE}`],
    checks: ["rate==1"],
    // 95 % of the target rate: k6 cannot always start every iteration exactly on time.
    http_reqs: [`rate>=${(RATE * 0.95).toFixed(2)}`],
    dropped_iterations: ["count==0"],
  },
};

function pick(items) {
  return items[Math.floor(Math.random() * items.length)];
}

export default function () {
  const roll = Math.random();
  let res;
  if (roll < 0.6) {
    const q = encodeURIComponent(pick(QUERIES));
    const page = 1 + Math.floor(Math.random() * 3);
    res = http.get(`${API}/catalogo/productos?q=${q}&pagina=${page}&tamanio=10`, { tags: { endpoint: "buscar" } });
  } else if (roll < 0.8) {
    res = http.get(`${API}/catalogo/productos/${pick(PRODUCT_IDS)}`, { tags: { endpoint: "detalle" } });
  } else if (roll < 0.9) {
    res = http.get(`${API}/catalogo/promociones`, { tags: { endpoint: "promociones" } });
  } else {
    res = http.get(`${API}/catalogo/disponibilidad?sku=${pick(SKUS)}&cantidad=1`, {
      tags: { endpoint: "disponibilidad" },
    });
  }
  check(res, { "catálogo responde 200": (r) => r.status === 200 });
}

export function handleSummary(data) {
  return buildSummary("catalogo", data);
}
