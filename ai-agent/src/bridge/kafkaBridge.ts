import { Kafka, type Producer } from "kafkajs";
import { loadConfig } from "../config.js";
import { logger, kafkaConsumeErrors, kafkaProduced } from "../observability.js";
import {
  AgentEventSchema,
  InboundCommandSchema,
  type AgentEvent,
  type InboundCommand,
  type Quote,
  QuoteSchema,
} from "../types.js";

export type CommandHandler = (cmd: InboundCommand) => Promise<void>;

// Maps eventType → specific topic. Falls back to agent.events for anything unrecognised.
function topicForEvent(eventType: string): keyof ReturnType<typeof loadConfig> {
  const map: Record<string, keyof ReturnType<typeof loadConfig>> = {
    field_captured:           "topicCallsFieldCaptured",
    quote_presented:          "topicCallsQuotePresented",
    objection_captured:       "topicCallsObjectionCaptured",
    consent_captured:         "topicCallsConsentCaptured",
    dnc_request:              "topicCallsDncRequested",
    live_transfer_requested:  "topicCallsLiveTransferRequested",
    call_ended:               "topicCallsEnded",
    transferred:              "topicCallsEnded",
  };
  return map[eventType] ?? "topicAgentEvents";
}

export class KafkaBridge {
  private kafka: Kafka;
  private producer: Producer;
  private commandHandler: CommandHandler | null = null;

  // correlationId → resolver for pending requestQuote calls
  private pendingQuotes: Map<string, (quote: Quote) => void> = new Map();
  private quoteTimeouts: Map<string, ReturnType<typeof setTimeout>> = new Map();

  constructor() {
    const cfg = loadConfig();
    this.kafka = new Kafka({
      brokers: cfg.kafkaBrokers.split(","),
      clientId: "ai-agent",
    });
    this.producer = this.kafka.producer({
      idempotent: true,
    });
  }

  async connect(): Promise<void> {
    await this.producer.connect();
  }

  async disconnect(): Promise<void> {
    await this.producer.disconnect();
  }

  setCommandHandler(handler: CommandHandler): void {
    this.commandHandler = handler;
  }

  async publishEvent(event: AgentEvent): Promise<void> {
    const cfg = loadConfig();
    const parsed = AgentEventSchema.parse(event);
    const topicKey = topicForEvent(parsed.eventType);
    const topic = cfg[topicKey] as string;

    try {
      await this.producer.send({
        topic,
        messages: [
          {
            key: parsed.leadId,
            value: JSON.stringify(parsed),
          },
        ],
      });
      kafkaProduced.inc({ topic });
    } catch (err) {
      logger.error({ err, event }, "failed to publish agent event, routing to DLQ");
      await this.routeToDlq(event, err);
    }
  }

  /**
   * Publish a quote request and wait for Python to respond on calls.quote_ready.
   * Resolves with the Quote when the correlated response arrives.
   * Rejects after 10 s to prevent the tool call from hanging forever.
   */
  async requestQuote(params: {
    correlationId: string;
    leadId: string;
    sessionId: string;
    productLine: string;
    age: number;
    state: string;
    faceValue?: number;
  }): Promise<Quote> {
    const cfg = loadConfig();

    return new Promise<Quote>((resolve, reject) => {
      // Register resolver before publishing (avoid race)
      this.pendingQuotes.set(params.correlationId, resolve);

      const timeout = setTimeout(() => {
        this.pendingQuotes.delete(params.correlationId);
        this.quoteTimeouts.delete(params.correlationId);
        reject(new Error(`quote request timed out for correlationId=${params.correlationId}`));
      }, 10_000);
      this.quoteTimeouts.set(params.correlationId, timeout);

      this.producer
        .send({
          topic: cfg.topicCallsQuoteRequested,
          messages: [
            {
              key: params.leadId,
              value: JSON.stringify(params),
            },
          ],
        })
        .then(() => kafkaProduced.inc({ topic: cfg.topicCallsQuoteRequested }))
        .catch((err) => {
          this.pendingQuotes.delete(params.correlationId);
          clearTimeout(this.quoteTimeouts.get(params.correlationId));
          this.quoteTimeouts.delete(params.correlationId);
          reject(err);
        });
    });
  }

  private resolveQuote(correlationId: string, quote: Quote): void {
    const resolver = this.pendingQuotes.get(correlationId);
    if (!resolver) return;
    this.pendingQuotes.delete(correlationId);
    clearTimeout(this.quoteTimeouts.get(correlationId));
    this.quoteTimeouts.delete(correlationId);
    resolver(quote);
  }

  private async routeToDlq(event: unknown, err: unknown): Promise<void> {
    const cfg = loadConfig();
    try {
      await this.producer.send({
        topic: cfg.topicDlq,
        messages: [
          {
            value: JSON.stringify({ event, error: String(err), ts: new Date().toISOString() }),
          },
        ],
      });
    } catch (dlqErr) {
      logger.error({ dlqErr }, "failed to route to DLQ");
    }
  }

  async startConsumer(): Promise<void> {
    const cfg = loadConfig();
    const consumer = this.kafka.consumer({ groupId: "ai-agent-commands" });
    await consumer.connect();
    await consumer.subscribe({
      topics: [cfg.topicLeadsRouted, cfg.topicCallsQuoteReady],
      fromBeginning: false,
    });

    await consumer.run({
      eachMessage: async ({ topic, partition, message }) => {
        if (!message.value) return;
        try {
          const raw = JSON.parse(message.value.toString());

          if (topic === cfg.topicCallsQuoteReady) {
            const { correlationId, quote } = raw as { correlationId: string; quote: unknown };
            if (correlationId) {
              const parsed = QuoteSchema.safeParse(quote);
              if (parsed.success) {
                this.resolveQuote(correlationId, parsed.data);
              } else {
                logger.warn({ correlationId, raw }, "received malformed quote_ready payload");
              }
            }
            return;
          }

          const cmd = InboundCommandSchema.safeParse(raw);
          if (!cmd.success) return;
          if (this.commandHandler) {
            await this.commandHandler(cmd.data);
          }
        } catch (err) {
          logger.error({ err, topic, partition }, "kafka consumer error");
          kafkaConsumeErrors.inc();
        }
      },
    });
  }
}
