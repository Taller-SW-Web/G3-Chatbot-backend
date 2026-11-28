// Shared config and report for the H5-P2 load tests. The SLO values live in
// G3-Chatbot-specs/docs/slo-performance.md: change them there first, then here.
import http from "k6/http";

export const BASE_URL = (__ENV.BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
export const API = `${BASE_URL}/api/v1`;
export const RESULTS_DIR = (__ENV.RESULTS_DIR || "results").replace(/\/$/, "");
// Sustained window of every scenario (SLO doc §2.4).
export const DURATION = __ENV.DURATION || "2m";

// The session endpoints check Origin and X-Requested-With (CSRF); the rest ignore them.
export const JSON_HEADERS = {
  "Content-Type": "application/json",
  Origin: __ENV.ORIGIN || "http://localhost:3000",
  "X-Requested-With": "XMLHttpRequest",
};

export const DEMO_EMAIL = __ENV.DEMO_EMAIL || "cliente@demo.pe";
export const DEMO_PASSWORD = __ENV.DEMO_PASSWORD || "Demo1234";

// p95 in ms per `endpoint` tag (SLO doc §2.1 to §2.3).
export const SLO_P95_MS = {
  buscar: 800,
  detalle: 800,
  promociones: 800,
  disponibilidad: 500,
  crear_conversacion: 500,
  turno: 6000,
  accion: 1500,
  historial: 800,
  agregar_carrito: 700,
  ver_carrito: 900,
  crear_checkout: 1500,
  pagar: 1000,
};
// Time to the first SSE byte (`aceptado`), SPEC-05 first fragment.
export const SLO_TURN_FIRST_BYTE_MS = 2000;
export const SLO_ERROR_RATE = 0.01;

export function latencyThresholds(endpoints) {
  const thresholds = {};
  for (const name of endpoints) {
    thresholds[`http_req_duration{endpoint:${name}}`] = [`p(95)<${SLO_P95_MS[name]}`];
  }
  return thresholds;
}

export function uuid() {
  // RFC 4122 v4 from Math.random: enough for Idempotency-Key in a load test.
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function login(email = DEMO_EMAIL, password = DEMO_PASSWORD) {
  const res = http.post(
    `${API}/sesion/login`,
    JSON.stringify({ email, password }),
    { headers: JSON_HEADERS, tags: { endpoint: "login" } },
  );
  if (res.status !== 200) {
    throw new Error(`login failed: ${res.status} ${res.body}`);
  }
  return res.json("accessToken");
}

export function authHeaders(token, extra = {}) {
  return { ...JSON_HEADERS, Authorization: `Bearer ${token}`, ...extra };
}

function formatValue(metric, key) {
  const value = metric.values[key];
  if (value === undefined) return "—";
  if (metric.type === "trend") return `${value.toFixed(0)} ms`;
  if (metric.type === "rate" && key === "rate") return `${(value * 100).toFixed(2)} %`;
  if (key === "rate") return `${value.toFixed(2)}/s`;
  return `${value}`;
}

// Writes `<name>-summary.json` and `<name>-report.md`: one row per threshold,
// measured value against the SLO, so the report is the H5-P3 evidence.
export function buildSummary(name, data) {
  const rows = [];
  let allOk = true;
  for (const [metricName, metric] of Object.entries(data.metrics)) {
    if (!metric.thresholds) continue;
    for (const [expression, result] of Object.entries(metric.thresholds)) {
      const measured = formatValue(metric, expression.split(/[<>=!]/)[0].trim());
      allOk = allOk && result.ok;
      rows.push(`| \`${metricName}\` | \`${expression}\` | ${measured} | ${result.ok ? "✅" : "❌"} |`);
    }
  }
  rows.sort();
  const reqs = data.metrics.http_reqs ? data.metrics.http_reqs.values : { count: 0, rate: 0 };
  const md = [
    `# k6 · ${name}`,
    "",
    `- Fecha: ${new Date().toISOString()}`,
    `- Base URL: ${BASE_URL}`,
    `- Solicitudes: ${reqs.count} (${reqs.rate.toFixed(2)}/s)`,
    `- Resultado: ${allOk ? "✅ cumple todos los SLO" : "❌ incumple al menos un SLO"}`,
    "",
    "| Métrica | SLO | Medido | Estado |",
    "|---|---|---|---|",
    ...rows,
    "",
  ].join("\n");
  return {
    stdout: `${md}\n`,
    [`${RESULTS_DIR}/${name}-summary.json`]: JSON.stringify(data, null, 2),
    [`${RESULTS_DIR}/${name}-report.md`]: md,
  };
}
