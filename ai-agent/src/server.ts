import Fastify from "fastify";
import cors from "@fastify/cors";
import websocket from "@fastify/websocket";
import { loadConfig } from "./config.js";
import { logger, registry } from "./observability.js";
import { KafkaBridge } from "./bridge/kafkaBridge.js";
import { RealtimeHandler } from "./realtime/realtimeHandler.js";
import { LeadContextSchema } from "./types.js";

async function main() {
  const cfg = loadConfig();

  const bridge = new KafkaBridge();
  await bridge.connect();

  const realtime = new RealtimeHandler(bridge);

  bridge.setCommandHandler(async (cmd) => {
    if (cmd.command === "start_session") {
      const sessionId = crypto.randomUUID();
      await realtime.createSession(sessionId, cmd.context);
    } else if (cmd.command === "end_session") {
      await realtime.endSession(cmd.sessionId, cmd.reason);
    }
  });

  await bridge.startConsumer();

  const app = Fastify({ logger: false });

  await app.register(cors);
  await app.register(websocket);

  app.post<{ Body: { lead_id: string; context: Record<string, unknown> } }>(
    "/sessions",
    async (request, reply) => {
      const { lead_id, context } = request.body;
      const lead = LeadContextSchema.parse({ ...context, leadId: lead_id });
      const sessionId = crypto.randomUUID();
      await realtime.createSession(sessionId, lead);
      return reply.code(201).send({ session_id: sessionId });
    }
  );

  app.post<{ Params: { id: string } }>(
    "/sessions/:id/end",
    async (request, reply) => {
      await realtime.endSession(request.params.id);
      return reply.send({ ended: true });
    }
  );

  app.get<{ Params: { id: string } }>(
    "/sessions/:id",
    async (request, reply) => {
      const session = realtime.getSession(request.params.id);
      if (!session) return reply.code(404).send({ error: "not found" });
      return reply.send(session);
    }
  );

  app.get("/health", async (_req, reply) => {
    return reply.send({ status: "ok" });
  });

  app.get("/metrics", async (_req, reply) => {
    const data = await registry.metrics();
    return reply
      .header("Content-Type", registry.contentType)
      .send(data);
  });

  app.register(async (instance) => {
    instance.get<{ Params: { id: string } }>(
      "/sessions/:id/media",
      { websocket: true },
      (socket, request) => {
        const sessionId = request.params.id;
        const bound = realtime.bindTelephony(sessionId, socket as any);
        if (!bound) {
          socket.close(1008, "session not found");
        }
      }
    );
  });

  await app.listen({ port: cfg.port, host: "0.0.0.0" });
  logger.info({ port: cfg.port }, "ai-agent listening");

  const shutdown = async () => {
    logger.info("shutting down");
    await bridge.disconnect();
    await app.close();
    process.exit(0);
  };

  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
