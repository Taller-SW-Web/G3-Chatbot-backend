// H5-P2 · Chat (mod-chat): 10 clientes anónimos durante DURATION (SLO doc §2.2 y §2.4).
// Requiere LLM_PROVIDER=fake. Cada VU tiene su propia cookie `chat_sid`, así que el
// límite de 20 mensajes/min se cuenta por VU; la espera de 3 a 5 s lo mantiene lejos.
import { check, sleep } from "k6";
import http from "k6/http";
import { Counter } from "k6/metrics";

import {
  API,
  DURATION,
  JSON_HEADERS,
  SLO_ERROR_RATE,
  SLO_TURN_FIRST_BYTE_MS,
  buildSummary,
  latencyThresholds,
} from "./lib/common.js";

const VUS = Number(__ENV.CHAT_VUS || 10);
const MIN_MESSAGES_PER_SECOND = Number(__ENV.CHAT_MIN_RATE || 1.5);
// Keywords only: the scripted LLM passes the text as `q` as is, without the real model's extraction.
const TEXTS = ["zapatillas running", "polo", "zapatillas nike", "mochila"];
const ACTIONS = [{ tipo: "VER_OFERTAS", payload: {} }, { tipo: "BUSCAR_PRODUCTOS", payload: { q: "zapatillas" } }];

const messages = new Counter("chat_mensajes");
const rateLimited = new Counter("chat_429");

export const options = {
  // k6 empties each VU's cookie jar at every iteration by default; without `chat_sid` the
  // conversation is no longer the VU's (404) and the limit falls back to the shared IP (429).
  noCookiesReset: true,
  scenarios: {
    chat: { executor: "constant-vus", vus: VUS, duration: DURATION },
  },
  thresholds: {
    ...latencyThresholds(["crear_conversacion", "turno", "accion", "historial"]),
    "http_req_waiting{endpoint:turno}": [`p(95)<${SLO_TURN_FIRST_BYTE_MS}`],
    http_req_failed: [`rate<${SLO_ERROR_RATE}`],
    checks: ["rate==1"],
    chat_mensajes: [`rate>=${MIN_MESSAGES_PER_SECOND}`],
    chat_429: ["count==0"],
  },
};

let conversationId = null;

function createConversation() {
  const res = http.post(`${API}/chat/conversaciones`, null, { tags: { endpoint: "crear_conversacion" } });
  check(res, { "conversación creada": (r) => r.status === 201 || r.status === 200 });
  return res.status < 300 ? res.json("conversacionId") : null;
}

function send(body, endpoint) {
  const res = http.post(`${API}/chat/conversaciones/${conversationId}/mensajes`, JSON.stringify(body), {
    headers: JSON_HEADERS,
    tags: { endpoint },
    timeout: "30s",
  });
  if (res.status === 429) rateLimited.add(1);
  messages.add(1);
  return res;
}

export default function () {
  // A fresh conversation every 10 iterations keeps the history short, as in a real session.
  if (conversationId === null || __ITER % 10 === 0) {
    conversationId = createConversation();
    if (conversationId === null) {
      sleep(1);
      return;
    }
  }
  if (Math.random() < 0.8) {
    const res = send({ texto: TEXTS[__ITER % TEXTS.length] }, "turno");
    check(res, {
      "turno 200": (r) => r.status === 200,
      "turno termina con fin": (r) => typeof r.body === "string" && r.body.includes("event: fin"),
      "turno trae productos de la herramienta": (r) =>
        typeof r.body === "string" && r.body.includes("CARRUSEL_PRODUCTOS"),
    });
  } else {
    const res = send({ accion: ACTIONS[__ITER % ACTIONS.length] }, "accion");
    check(res, { "acción 200": (r) => r.status === 200 });
  }
  const history = http.get(`${API}/chat/conversaciones/${conversationId}/mensajes`, {
    tags: { endpoint: "historial" },
  });
  check(history, { "historial 200": (r) => r.status === 200 });
  sleep(3 + Math.random() * 2);
}

export function handleSummary(data) {
  return buildSummary("chat", data);
}
